"""题库服务 — 生成(走graph)、答题、掌握度追踪"""

import json
import logging
import re
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

from tortoise.expressions import Q

from backend.src.models.exam_model import ExamQuestion, ExamRecord, KnowledgeMastery
from backend.src.models.resource_model import GeneratedResource
from backend.src.models.usermodel import User
from backend.src.utils.database import init_db
from backend.src.utils.json_parser import parse_llm_json
from backend.src.service.notification.service import check_and_create_ai_tip
from backend.src.service.portrait.service import PortraitRadarService


def _normalize_db_answer(raw: str) -> str:
    """归一化 DB 中存储的答案，消除 LLM 格式漂移。

    DB 里答案可能是: "A" / '"A"' / '["A"]' / "A. xxx" / "(A)" / "（A）" / true / "True"
    统一转为大写字母，用于与用户提交的答案比较。
    判断题 true/false → A/B（A=正确 B=错误）
    """
    if not raw:
        return ""
    text = raw.strip()
    # 尝试 JSON 解析（处理 ["A"] / "A" / true / false 等 JSON 编码）
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return ",".join(sorted(str(x).strip().upper() for x in parsed if str(x).strip()))
        # JSON 布尔值 → 映射为选项 A/B（判断题 A=正确 B=错误）
        if isinstance(parsed, bool):
            return "A" if parsed else "B"
        text = str(parsed)
    except (json.JSONDecodeError, TypeError):
        logger.debug("答案不是 JSON 格式，按普通文本解析 value=%r", text, exc_info=True)
    # 字符串 true/false → A/B
    upper = text.strip().upper()
    if upper in ("TRUE", "FALSE"):
        return "A" if upper == "TRUE" else "B"
    # 去掉选项文本和括号只留字母
    # 例: "A. xxx" → "A", "(A)" → "A", "（B）" → "B", "A " → "A"
    m = re.search(r'[（(]?\s*([A-D])\s*[）).、]?', text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return upper


def _parse_multi_ans(ans: str) -> set:
    """解析多选题答案字符串为字母集合"""
    text = ans.strip()
    if text.startswith("["):
        try:
            return set(str(x).strip().upper() for x in json.loads(text))
        except (json.JSONDecodeError, TypeError):
            logger.debug("多选答案不是 JSON 数组，按逗号分隔解析 value=%r", text, exc_info=True)
    return set(text.upper().replace(" ", "").split(","))


def _weight(difficulty: str, question_type: str) -> float:
    """题目权重：easy=1, medium=2, hard=3，多选 +0.5"""
    base = {"easy": 1.0, "medium": 2.0, "hard": 3.0}.get(difficulty, 2.0)
    if (question_type or "").lower() == "multi_choice":
        base += 0.5
    return base


def _normalize_score(weight: float, total_weight: float) -> float:
    """将权重转为百分制分数"""
    if total_weight == 0:
        return 0.0
    return round(weight / total_weight * 100, 1)


def _question_to_dict(q: ExamQuestion) -> dict:
    def _safe_parse(text: str | None):
        if not text:
            return text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    return {
        "question_id": q.id,
        "question_type": q.question_type,
        "content": q.content,
        "options": _safe_parse(q.options),
        "answer": _safe_parse(q.answer),
        "analysis": q.analysis,
        "difficulty": q.difficulty,
        "knowledge_tags": _safe_parse(q.knowledge_tags) or [],
        "weight": 1.0,
        "created_at": str(q.created_at),
    }


class ExamService:

    @staticmethod
    async def _save_questions(questions: list[dict], user, difficulty: str, node_id: int | None = None) -> tuple[str, list[dict]]:
        """将解析好的题目存库 + 创建 ExamRecord 占位，返回 (session_id, questions)"""
        if not questions:
            return str(uuid.uuid4())[:12], []
        session_id = str(uuid.uuid4())[:12]
        logger.info("_save_questions session_id=%r node_id=%r count=%d", session_id, node_id, len(questions))
        saved = []
        for q in questions:
            qt = (q.get("question_type", "single_choice") or "").lower()
            diff = q.get("difficulty", difficulty)
            pv = 1.0
            record = await ExamQuestion.create(
                question_type=qt,
                content=q.get("content", ""),
                options=json.dumps(q.get("options"), ensure_ascii=False) if q.get("options") else None,
                answer=json.dumps(q.get("answer"), ensure_ascii=False) if isinstance(q.get("answer"), list) else str(q.get("answer") if q.get("answer") is not None else ""),
                analysis=q.get("analysis", ""),
                difficulty=diff,
                knowledge_tags=json.dumps(q.get("knowledge_tags", []), ensure_ascii=False),
                point_value=pv,
                user=user,
            )
            # 创建占位记录，使 session 立即可查询
            await ExamRecord.create(
                question=record,
                user_id=user.id,
                session_id=session_id,
                node_id=node_id,
            )
            saved.append(_question_to_dict(record))
        return session_id, saved

    @staticmethod
    async def generate_and_save(
        topic: str, user_id: int,
        question_types: list[str] | None = None, count: int = 10, difficulty: str = "medium",
        node_id: int | None = None, user_notes: str = "", chat_group_id: int = 0,
        skip_review: bool = False, llm_priority: str = "high",
        include_request_in_history: bool = True,
    ) -> dict:
        """走 graph 出题（Leader→Executor→Reviewer→retry）→ 存库 → 返回 session_id + questions"""
        await init_db()

        user = await User.filter(id=user_id).first()
        if not user:
            return {"session_id": None, "questions": []}

        from backend.src.service.resource.service import ResourceService  # deferred: circular exam<->resource

        types = question_types or ["single_choice", "multi_choice", "true_false", "fill_blank"]
        types_str = ", ".join(types)

        saved_resources = await ResourceService.generate_and_save(
            topic=topic, user_id=user_id, resource_types=["exercise"],
            exam_question_types=types_str, exam_count=count, exam_difficulty=difficulty,
            chat_group_id=chat_group_id, user_notes=user_notes,
            skip_review=skip_review, llm_priority=llm_priority,
            include_request_in_history=include_request_in_history,
        )

        for r in saved_resources:
            if r.get("resource_type") == "exercise":
                # ResourceService 可能已保存题目并附带 session_id → 直接复用，避免重复存库
                existing_session_id = r.get("session_id")
                if existing_session_id:
                    # 修正占位记录的 node_id（ResourceService 存库时不知道 node_id）
                    if node_id:
                        await ExamRecord.filter(session_id=existing_session_id, node_id__isnull=True).update(node_id=node_id)
                    records = await ExamRecord.filter(session_id=existing_session_id).prefetch_related("question").all()
                    saved = [_question_to_dict(rec.question) for rec in records if rec.question]
                    return {"session_id": existing_session_id, "questions": saved}

                content = r.get("content", "")
                try:
                    questions = parse_llm_json(content)
                    if not isinstance(questions, list):
                        questions = []
                except Exception:
                    logger.exception("题目 JSON 解析失败 resource_id=%s", r.get("resource_id"))
                    questions = []
                if not questions:
                    return {"session_id": None, "questions": []}

                # 仅按题型和数量过滤，难度分布由 learning_guidance 控制
                allowed_types = question_types or ["single_choice", "multi_choice", "true_false"]
                allowed_lower = {t.lower() for t in allowed_types}
                filtered = [q for q in questions if (q.get("question_type") or "").lower() in allowed_lower]
                filtered = filtered[:count]

                session_id, saved = await ExamService._save_questions(filtered, user, difficulty, node_id=node_id)
                return {"session_id": session_id, "questions": saved}

        return {"session_id": None, "questions": []}

    @staticmethod
    async def generate_and_save_stream(
        topic: str, user_id: int,
        question_types: list[str] | None = None, count: int = 10, difficulty: str = "medium",
        node_id: int | None = None, user_notes: str = "", chat_group_id: int = 0,
    ):
        """流式走 graph 出题 → SSE 推送进度 → 存库 → 返回 session"""
        await init_db()

        user = await User.filter(id=user_id).first()
        if not user:
            yield f"data: {json.dumps({'type': 'error', 'detail': '用户不存在'}, ensure_ascii=False)}\n\n"
            return

        from backend.src.service.resource.service import ResourceService  # deferred: circular exam<->resource

        types = question_types or ["single_choice", "multi_choice", "true_false", "fill_blank"]
        types_str = ", ".join(types)

        yield f"data: {json.dumps({'type': 'status', 'msg': '正在分析知识点并生成题目...'}, ensure_ascii=False)}\n\n"

        async for event in ResourceService.generate_stream(
            topic=topic, user_id=user_id, resource_types=["exercise"],
            chat_group_id=chat_group_id,
            exam_question_types=types_str, exam_count=count, exam_difficulty=difficulty,
            user_notes=user_notes,
        ):
            if isinstance(event, str) and event.startswith("data:"):
                data_str = event[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    payload = json.loads(data_str)
                    if payload.get("type") == "file":
                        rt = payload.get("file_type", "")
                        if rt == "exercise":
                            yield f"data: {json.dumps({'type': 'progress', 'msg': '题目内容已生成，正在审核...'}, ensure_ascii=False)}\n\n"
                    elif "review_passed" in payload:
                        passed = payload.get("review_passed", True)
                        yield f"data: {json.dumps({'type': 'progress', 'msg': f'审核{"通过" if passed else "未通过，重新生成"}...'}, ensure_ascii=False)}\n\n"
                except json.JSONDecodeError:
                    logger.warning("题目流式事件 JSON 解析失败 raw=%r", data_str[:300], exc_info=True)

        # 查已保存的 exercise 资源，解析题目并保存
        saved_resources = await GeneratedResource.filter(
            user_id=user_id, topic=topic, resource_type="exercise"
        ).order_by("-created_at").limit(1).all()

        session_id = None
        saved_questions = []
        for r in saved_resources:
            if r.content:
                try:
                    questions = parse_llm_json(r.content)
                    if not isinstance(questions, list):
                        questions = []
                except Exception:
                    logger.exception("题目 JSON 解析失败 resource_id=%s", r.id)
                    questions = []
                if questions:
                    # 仅按题型和数量过滤，难度分布由 learning_guidance 控制
                    types_lower = {t.lower() for t in types}
                    filtered = [q for q in questions if (q.get("question_type") or "").lower() in types_lower]
                    filtered = filtered[:count]
                    if filtered:
                        session_id, saved_questions = await ExamService._save_questions(
                            filtered, user, difficulty, node_id=node_id
                        )
                        yield f"data: {json.dumps({'type': 'progress', 'msg': f'已保存 {len(saved_questions)} 道题目'}, ensure_ascii=False)}\n\n"
                break

        yield f"data: {json.dumps({'type': 'done', 'session_id': session_id, 'quiz_config': {'count': count, 'threshold': 0.7}, 'question_count': len(saved_questions)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    @staticmethod
    async def list_questions(user_id: int, question_type: str | None = None, difficulty: str | None = None, knowledge_tag: str | None = None, node_id: int | None = None, page: int = 1, page_size: int = 20) -> dict:
        if node_id:
            # 仅返回该节点关联的题目（通过 ExamRecord 绑定）
            record_qs = ExamRecord.filter(node_id=node_id).values_list("question_id", flat=True)
            question_ids = [rid async for rid in record_qs]
            if not question_ids:
                return {"total": 0, "page": page, "page_size": page_size, "items": []}
            qs = ExamQuestion.filter(id__in=question_ids)
        else:
            qs = ExamQuestion.filter(Q(is_public=True) | Q(user_id=user_id))
        if question_type:
            qs = qs.filter(question_type=question_type)
        if difficulty:
            qs = qs.filter(difficulty=difficulty)
        if knowledge_tag:
            qs = qs.filter(knowledge_tags__contains=knowledge_tag)
        total = await qs.count()
        records = await qs.order_by("-created_at").offset((page - 1) * page_size).limit(page_size).all()
        return {
            "total": total, "page": page, "page_size": page_size,
            "items": [_question_to_dict(r) for r in records],
        }

    @staticmethod
    async def get_question(question_id: int, user_id: int) -> dict | None:
        record = await ExamQuestion.filter(id=question_id).first()
        if not record:
            return None
        if not record.is_public and (record.user_id is None or record.user_id != user_id):
            return None
        return _question_to_dict(record)

    @staticmethod
    async def delete_question(question_id: int, user_id: int) -> bool:
        record = await ExamQuestion.filter(id=question_id, user_id=user_id).first()
        if not record:
            return False
        await record.delete()
        return True

    @staticmethod
    async def submit_answer(question_id: int, user_id: int, user_answer: str, time_spent: int | None = None, session_id: str | None = None, node_id: int | None = None) -> dict:
        question = await ExamQuestion.filter(id=question_id).first()
        if not question:
            raise ValueError("题目不存在")

        # 归一化 DB 中的答案（LLM 输出格式不稳定：A / "A" / ["A"] / a）
        correct_answer = _normalize_db_answer(question.answer)

        logger.info(
            "submit_answer qid=%s type=%s raw_db_answer=%r normalized_answer=%r user_answer=%r session_id=%r node_id=%r",
            question_id, question.question_type, question.answer, correct_answer, user_answer, session_id, node_id,
        )

        # 判断对错（每题等权重 1 分）
        qt = (question.question_type or "").lower()
        if qt == "multi_choice":
            try:
                user_set = _parse_multi_ans(user_answer)
                correct_set = _parse_multi_ans(correct_answer)
                is_correct = (user_set == correct_set)
            except Exception:
                is_correct = user_answer.strip().upper() == correct_answer
        else:
            is_correct = (user_answer.strip().upper() == correct_answer)

        score = 1.0 if is_correct else 0.0

        sid = session_id or str(uuid.uuid4())[:12]

        # 复用占位记录，避免 total_weight 重复计算
        existing = await ExamRecord.filter(
            user_id=user_id, session_id=sid, question_id=question_id
        ).order_by("-id").first()
        if existing:
            existing.user_answer = user_answer
            existing.is_correct = is_correct
            existing.score = score
            existing.time_spent = time_spent
            if node_id:
                existing.node_id = node_id
            elif existing.node_id:
                node_id = existing.node_id
            await existing.save()
        else:
            await ExamRecord.create(
                question=question,
                user_id=user_id,
                user_answer=user_answer,
                is_correct=is_correct,
                score=score,
                time_spent=time_spent,
                session_id=sid,
                node_id=node_id,
            )

        # 更新知识点掌握度
        if question.knowledge_tags:
            try:
                tags = json.loads(question.knowledge_tags)
            except (json.JSONDecodeError, TypeError):
                logger.warning("知识点标签 JSON 解析失败 question_id=%s", question_id)
                tags = []
            for tag in tags:
                mastery, _ = await KnowledgeMastery.get_or_create(
                    user_id=user_id, knowledge_tag=tag,
                    defaults={"total_attempts": 0, "correct_count": 0, "mastery_level": "beginner", "last_practiced_at": datetime.now()},
                )
                mastery.total_attempts += 1
                if is_correct:
                    mastery.correct_count += 1
                rate = mastery.correct_count / max(mastery.total_attempts, 1)
                if rate >= 0.9:
                    mastery.mastery_level = "mastered"
                elif rate >= 0.7:
                    mastery.mastery_level = "proficient"
                elif rate >= 0.4:
                    mastery.mastery_level = "learning"
                else:
                    mastery.mastery_level = "beginner"
                mastery.last_practiced_at = datetime.now()
                await mastery.save()

        await check_and_create_ai_tip(user_id)

        from backend.src.service.path.helpers import update_portrait_from_mastery
        try:
            await update_portrait_from_mastery(user_id)
        except Exception:
            logger.exception("答题后画像同步失败 user_id=%s", user_id)

        try:
            await PortraitRadarService.compute(user_id)
            await PortraitRadarService.sync_to_portrait(user_id)
        except Exception:
            logger.exception("雷达即时刷新失败 user_id=%s", user_id)

        # 汇总本轮会话成绩（总分恒为 100）
        raw_session_records = await ExamRecord.filter(user_id=user_id, session_id=sid).order_by("id").prefetch_related("question").all()
        latest_by_question = {}
        for r in raw_session_records:
            latest_by_question[r.question_id] = r
        session_records = list(latest_by_question.values())
        judged = [r for r in session_records if r.is_correct is not None]
        total_questions = len(session_records)
        correct_count = sum(1 for r in judged if r.is_correct)
        judged_count = len(judged)
        total_weight = sum(
            float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
            for r in session_records if r.question
        )
        earned_weight = sum(float(r.score) for r in session_records if r.score is not None)
        # 当前题目的百分制得分
        question_score = _normalize_score(score or 0.0, total_weight) if total_weight > 0 else None

        return {
            "question_id": question_id,
            "is_correct": is_correct,
            "score": question_score,           # 百分制得分
            "weight": _weight(question.difficulty, question.question_type),
            "correct_answer": correct_answer if not is_correct else None,
            "analysis": question.analysis if not is_correct else None,
            "session_id": sid,
            "session_summary": {
                "total_questions": total_questions,
                "correct_count": correct_count,
                "incorrect_count": judged_count - correct_count,
                "pending_count": total_questions - judged_count,
                "total_points": 100,            # 总分恒为 100
                "earned_points": _normalize_score(earned_weight, total_weight) if total_weight > 0 else 0.0,
                "percentage": round(earned_weight / total_weight * 100, 1) if total_weight > 0 else None,
            },
        }

    @staticmethod
    async def get_records(user_id: int, node_id: int | None = None, page: int = 1, page_size: int = 20) -> dict:
        qs = ExamRecord.filter(user_id=user_id)
        if node_id:
            qs = qs.filter(node_id=node_id)
        total = await qs.count()
        records = await qs.order_by("-created_at").offset((page - 1) * page_size).limit(page_size).prefetch_related("question").all()
        items = []
        for r in records:
            items.append({
                "record_id": r.id,
                "question": _question_to_dict(r.question) if r.question else None,
                "user_answer": r.user_answer,
                "is_correct": r.is_correct,
                "score": r.score,
                "time_spent": r.time_spent,
                "session_id": r.session_id,
                "created_at": str(r.created_at),
            })
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    @staticmethod
    async def get_session(session_id: str, user_id: int) -> dict | None:
        """查询一次练习会话的完整状态：所有答题记录 + 汇总"""
        raw_records = await (
            ExamRecord.filter(session_id=session_id, user_id=user_id)
            .order_by("id")
            .prefetch_related("question")
            .all()
        )
        latest_by_question = {}
        for r in raw_records:
            latest_by_question[r.question_id] = r
        records = list(latest_by_question.values())
        if not records:
            return None

        items = []
        for r in records:
            items.append({
                "record_id": r.id,
                "question": _question_to_dict(r.question) if r.question else None,
                "user_answer": r.user_answer,
                "is_correct": r.is_correct,
                "score": r.score,
                "time_spent": r.time_spent,
                "created_at": str(r.created_at),
            })

        judged = [r for r in records if r.is_correct is not None]
        correct_count = sum(1 for r in judged if r.is_correct)
        judged_count = len(judged)
        total_weight = sum(
            float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
            for r in records if r.question
        )
        earned_weight = sum(float(r.score) for r in records if r.score is not None)

        return {
            "session_id": session_id,
            "total_questions": len(records),
            "correct_count": correct_count,
            "incorrect_count": judged_count - correct_count,
            "pending_count": len(records) - judged_count,
            "total_points": 100,
            "earned_points": _normalize_score(earned_weight, total_weight) if total_weight > 0 else 0.0,
            "percentage": round(earned_weight / total_weight * 100, 1) if total_weight > 0 else None,
            "records": items,
        }

    @staticmethod
    async def list_sessions(user_id: int) -> list[dict]:
        """列出用户的所有练习会话摘要"""
        records = await (
            ExamRecord.filter(user_id=user_id)
            .order_by("-created_at")
            .prefetch_related("question")
            .all()
        )

        sessions: dict[str, dict] = {}
        for r in records:
            if r.session_id not in sessions:
                sessions[r.session_id] = {
                    "session_id": r.session_id,
                    "total": 0,
                    "correct": 0,
                    "judged": 0,
                    "total_weight": 0.0,
                    "earned_weight": 0.0,
                    "first_at": str(r.created_at),
                    "last_at": str(r.created_at),
                }
            s = sessions[r.session_id]
            s["total"] += 1
            if r.question:
                s["total_weight"] += float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
            if r.score is not None:
                s["earned_weight"] += float(r.score)
                s["judged"] += 1
            if r.is_correct is True:
                s["correct"] += 1
            elif r.is_correct is False:
                pass
            s["last_at"] = str(r.created_at)

        for s in sessions.values():
            s["score"] = round(s["correct"] / s["judged"] * 100, 1) if s["judged"] else None
            s["percentage"] = round(s["earned_weight"] / s["total_weight"] * 100, 1) if s["total_weight"] > 0 else None
            s["total_points"] = 100

        return sorted(sessions.values(), key=lambda x: x["last_at"], reverse=True)

    @staticmethod
    async def get_statistics(user_id: int) -> dict:
        """用户整体答题统计（总分恒为 100）"""
        records = await ExamRecord.filter(user_id=user_id).prefetch_related("question").all()
        judged = [r for r in records if r.is_correct is not None]

        if not judged:
            return {
                "total_answered": 0, "total_correct": 0,
                "total_points": 100, "earned_points": 0,
                "overall_accuracy": None, "overall_percentage": None,
                "by_difficulty": {}, "by_type": {}, "by_knowledge_tag": [],
            }

        total_correct = sum(1 for r in judged if r.is_correct)
        total_weight = sum(
            float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
            for r in judged if r.question
        )
        earned_weight = sum(float(r.score) for r in judged if r.score is not None)

        def _build_breakdown(items: list, key_fn) -> dict:
            buckets: dict[str, dict] = {}
            for r in items:
                if not r.question:
                    continue
                k = key_fn(r.question)
                if k not in buckets:
                    buckets[k] = {"total": 0, "correct": 0, "total_weight": 0.0, "earned_weight": 0.0}
                buckets[k]["total"] += 1
                buckets[k]["total_weight"] += float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
                if r.score is not None:
                    buckets[k]["earned_weight"] += float(r.score)
                if r.is_correct:
                    buckets[k]["correct"] += 1
            for v in buckets.values():
                v["accuracy"] = round(v["correct"] / v["total"] * 100, 1) if v["total"] else None
                v["percentage"] = round(v["earned_weight"] / v["total_weight"] * 100, 1) if v["total_weight"] > 0 else None
            return buckets

        by_difficulty = _build_breakdown(judged, lambda q: q.difficulty)
        by_type = _build_breakdown(judged, lambda q: q.question_type)

        # 按知识点统计（Top 20）
        tag_stats: dict[str, dict] = {}
        for r in judged:
            if not r.question or not r.question.knowledge_tags:
                continue
            try:
                tags = json.loads(r.question.knowledge_tags)
            except (json.JSONDecodeError, TypeError):
                continue
            w = float(r.question.point_value or _weight(r.question.difficulty, r.question.question_type))
            for tag in tags:
                if tag not in tag_stats:
                    tag_stats[tag] = {"knowledge_tag": tag, "total": 0, "correct": 0, "total_weight": 0.0, "earned_weight": 0.0}
                tag_stats[tag]["total"] += 1
                tag_stats[tag]["total_weight"] += w
                if r.score is not None:
                    tag_stats[tag]["earned_weight"] += float(r.score)
                if r.is_correct:
                    tag_stats[tag]["correct"] += 1
        for t in tag_stats.values():
            t["accuracy"] = round(t["correct"] / t["total"] * 100, 1) if t["total"] else None
            t["percentage"] = round(t["earned_weight"] / t["total_weight"] * 100, 1) if t["total_weight"] > 0 else None
        by_tag = sorted(tag_stats.values(), key=lambda x: x["total"], reverse=True)[:20]

        return {
            "total_answered": len(judged),
            "total_correct": total_correct,
            "total_points": 100,
            "earned_points": _normalize_score(earned_weight, total_weight) if total_weight > 0 else 0.0,
            "overall_accuracy": round(total_correct / len(judged) * 100, 1),
            "overall_percentage": round(earned_weight / total_weight * 100, 1) if total_weight > 0 else None,
            "by_difficulty": by_difficulty,
            "by_type": by_type,
            "by_knowledge_tag": by_tag,
        }

    @staticmethod
    async def get_mastery(user_id: int) -> list[dict]:
        records = await KnowledgeMastery.filter(user_id=user_id).order_by("-last_practiced_at").all()
        return [
            {
                "knowledge_tag": r.knowledge_tag,
                "total_attempts": r.total_attempts,
                "correct_count": r.correct_count,
                "accuracy": round(r.correct_count / max(r.total_attempts, 1), 2),
                "mastery_level": r.mastery_level,
                "last_practiced_at": str(r.last_practiced_at),
            }
            for r in records
        ]
