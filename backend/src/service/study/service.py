"""学习统计服务 — 心跳、汇总、资源已读"""

import json
import logging
from datetime import date, datetime, timedelta

from backend.src.models.study_model import StudySession, ResourceReadStatus, ResourceCollection
from backend.src.models.exam_model import KnowledgeMastery, ExamRecord
from backend.src.models.resource_model import GeneratedResource
from backend.src.models.path_model import LearningPath, PathNode, UserPathProgress
from backend.src.service.portrait.service import build_learning_guidance, PortraitRadarService
from backend.src.service.notification.service import check_and_create_weekly_report

logger = logging.getLogger(__name__)


class StudyService:

    @staticmethod
    async def heartbeat(user_id: int, path_id: int | None = None) -> dict:
        """前端每 30 秒调用一次，累计今日学习时长。path_id 可选，用于分路径统计"""
        today = date.today()
        try:
            session, _ = await StudySession.get_or_create(
                user_id=user_id, date=today,
                defaults={"total_seconds": 0, "last_heartbeat_at": datetime.now()},
            )
        except Exception:
            session = await StudySession.filter(user_id=user_id, date=today).first()
            if not session:
                raise
        session.total_seconds += 30
        session.last_heartbeat_at = datetime.now()
        if path_id is not None:
            session.path_id = path_id
        await session.save()
        return {"today_seconds": session.total_seconds}

    @staticmethod
    async def mark_read(user_id: int, resource_id: int, duration_seconds: int = 0) -> dict:
        """标记资源为已读，可选上报使用时长"""
        resource = await GeneratedResource.filter(id=resource_id).first()
        if not resource:
            raise ValueError("资源不存在")
        status, created = await ResourceReadStatus.get_or_create(
            user_id=user_id, resource_id=resource_id,
            defaults={"is_read": True, "read_at": datetime.now(), "duration_seconds": max(duration_seconds, 0)},
        )
        if not created:
            if not status.is_read:
                status.is_read = True
                status.read_at = datetime.now()
            if duration_seconds > 0:
                status.duration_seconds += duration_seconds
            await status.save()
        return {"resource_id": resource_id, "is_read": True, "duration_seconds": status.duration_seconds}

    @staticmethod
    async def mark_unread(user_id: int, resource_id: int) -> dict:
        """标记资源为未读"""
        resource = await GeneratedResource.filter(id=resource_id).first()
        if not resource:
            raise ValueError("资源不存在")
        status = await ResourceReadStatus.filter(user_id=user_id, resource_id=resource_id).first()
        if status:
            status.is_read = False
            status.read_at = None
            await status.save()
        return {"resource_id": resource_id, "is_read": False}

    @staticmethod
    async def get_stats(user_id: int) -> dict:
        """聚合学习统计"""

        # 检查并生成周报
        await check_and_create_weekly_report(user_id)

        # ── 学习时长 ──
        today = date.today()
        week_ago = today - timedelta(days=6)

        sessions = await StudySession.filter(user_id=user_id, date__gte=week_ago).all()
        today_session = next((s for s in sessions if s.date == today), None)
        today_seconds = today_session.total_seconds if today_session else 0
        week_seconds = sum(s.total_seconds for s in sessions)
        active_days = len({s.date for s in sessions if s.total_seconds > 0})

        all_sessions = await StudySession.filter(user_id=user_id).all()
        total_seconds = sum(s.total_seconds for s in all_sessions)

        # ── 薄弱点（知识点 + 雷达维度） ──
        mastery_records = await KnowledgeMastery.filter(user_id=user_id).order_by("-last_practiced_at").all()
        weak_points = [
            {
                "tag": r.knowledge_tag,
                "accuracy": round(r.correct_count / max(r.total_attempts, 1), 2),
                "level": r.mastery_level,
                "total_attempts": r.total_attempts,
                "source": "mastery",
            }
            for r in mastery_records
            if r.mastery_level in ("beginner", "learning")
        ]
        # 追加雷达弱项维度
        try:
            radar = await PortraitRadarService.get(user_id)
            if radar and radar.get("dimensions"):
                labels = {"memory": "记忆(简单题)", "understanding": "理解(中等题)", "application": "应用(困难题)",
                          "analysis": "分析(多选题)", "breadth": "广度(知识覆盖)", "persistence": "坚持(活跃度)"}
                for d in radar["dimensions"]:
                    if d["score"] < 50:
                        weak_points.append({
                            "tag": labels.get(d["key"], d["key"]),
                            "accuracy": round(d["score"] / 100, 2),
                            "level": "beginner" if d["score"] < 40 else "learning",
                            "total_attempts": 0,
                            "source": "radar",
                        })
        except Exception:
            logger.exception("雷达弱项读取失败 user_id=%s", user_id)

        # ── 学习路径 ──
        paths = []
        all_progress_records = await UserPathProgress.filter(user_id=user_id).all()
        progress_by_path: dict[int, list] = {}
        for record in all_progress_records:
            progress_by_path.setdefault(record.path_id, []).append(record)

        path_ids = list(progress_by_path.keys())
        path_records = await LearningPath.filter(id__in=path_ids).prefetch_related("nodes").all() if path_ids else []
        for p in path_records:
            nodes = sorted(list(p.nodes or []), key=lambda n: n.order_index)
            node_order = {n.id: n.order_index for n in nodes}
            node_map = {n.id: n for n in nodes}
            progress_records = sorted(
                progress_by_path.get(p.id, []),
                key=lambda r: node_order.get(r.node_id, 10**9),
            )
            total_nodes = len(nodes)
            completed_nodes = sum(1 for r in progress_records if r.node_status == "completed")
            current = None
            for r in progress_records:
                if r.node_status in ("unlocked", "in_progress"):
                    node = node_map.get(r.node_id)
                    current = node.topic if node else None
                    break
            paths.append({
                "path_id": p.id,
                "goal": p.subject or "",
                "progress": round(completed_nodes / max(total_nodes, 1), 2),
                "total_nodes": total_nodes,
                "completed_nodes": completed_nodes,
                "current_node": current,
            })

        # ── 资源已读 + 使用率 ──
        resources = await GeneratedResource.filter(user_id=user_id).all()
        read_statuses = await ResourceReadStatus.filter(user_id=user_id,
            resource_id__in=[r.id for r in resources]).all()
        read_map = {rs.resource_id: rs.is_read for rs in read_statuses}
        collections = await ResourceCollection.filter(user_id=user_id).all()
        collected_ids = {c.resource_id for c in collections}

        by_type: dict[str, dict] = {}
        total_read = 0
        total_views = 0
        total_downloads = 0
        for r in resources:
            rt = r.resource_type or "other"
            if rt not in by_type:
                by_type[rt] = {"total": 0, "read": 0}
            by_type[rt]["total"] += 1
            if read_map.get(r.id):
                by_type[rt]["read"] += 1
                total_read += 1
            total_views += (r.view_count or 0)
            total_downloads += (r.download_count or 0)

        # ── 答题汇总 ──
        exam_records = await ExamRecord.filter(user_id=user_id).all()
        total_questions = len(exam_records)
        judged_records = [r for r in exam_records if r.is_correct is not None]
        completed_questions = len(judged_records)
        correct_count = sum(1 for r in judged_records if r.is_correct)
        session_ids = {r.session_id for r in exam_records if r.session_id}
        # 练习完成率：judged > 0 的会话视为完成
        session_totals: dict[str, dict[str, int]] = {}
        for r in exam_records:
            sid = r.session_id
            if not sid:
                continue
            if sid not in session_totals:
                session_totals[sid] = {"total": 0, "judged": 0}
            session_totals[sid]["total"] += 1
            if r.is_correct is not None:
                session_totals[sid]["judged"] += 1
        completed_sessions = sum(
            1 for item in session_totals.values()
            if item["total"] > 0 and item["judged"] >= item["total"]
        )
        total_sessions = len(session_ids)

        # ── 学习指导 ──
        guidance = ""
        try:
            guidance = await build_learning_guidance(user_id)
        except Exception:
            logger.exception("学习指导生成失败 user_id=%s", user_id)

        return {
            "study_time": {
                "today_seconds": today_seconds,
                "week_seconds": week_seconds,
                "total_seconds": total_seconds,
                "active_days": active_days,
            },
            "weak_points": weak_points,
            "learning_paths": paths,
            "resources": {
                "total": len(resources),
                "read_count": total_read,
                "unread_count": len(resources) - total_read,
                "open_rate": round(total_read / max(len(resources), 1), 2),
                "total_views": total_views,
                "total_downloads": total_downloads,
                "collected_count": len(collections),
                "by_type": by_type,
            },
            "exam_summary": {
                "total_questions": total_questions,
                "completed_questions": completed_questions,
                "correct_questions": correct_count,
                "correct_rate": round(correct_count / max(completed_questions, 1), 2),
                "total_sessions": total_sessions,
                "completed_sessions": completed_sessions,
                "completion_rate": round(completed_questions / max(total_questions, 1), 2),
                "session_completion_rate": round(completed_sessions / max(total_sessions, 1), 2),
            },
            "learning_guidance": guidance,
        }

    @staticmethod
    async def get_path_stats(user_id: int) -> dict:
        """分路径统计：学习时长、进度、薄弱点"""
        paths_result = []

        # 用户已加入的路径
        enrolled_progress = await UserPathProgress.filter(user_id=user_id).values("path_id")
        path_ids = list({p["path_id"] for p in enrolled_progress})
        if not path_ids:
            return {"paths": [], "untracked_time": {}, "total_time": 0, "overall_weak_points": []}
        path_records = await LearningPath.filter(id__in=path_ids).prefetch_related("nodes").all()

        # ── 学习时长：按 path_id 分组 ──
        sessions = await StudySession.filter(user_id=user_id).all()
        path_time: dict[int, int] = {}
        total_time = 0
        today = date.today()
        today_path_time: dict[int, int] = {}
        week_ago = today - timedelta(days=6)
        week_path_time: dict[int, int] = {}

        for s in sessions:
            if s.total_seconds > 0:
                total_time += s.total_seconds
                pid = s.path_id or 0
                path_time[pid] = path_time.get(pid, 0) + s.total_seconds
                if s.date and s.date >= week_ago:
                    week_path_time[pid] = week_path_time.get(pid, 0) + s.total_seconds
                if s.date == today:
                    today_path_time[pid] = today_path_time.get(pid, 0) + s.total_seconds

        # ── 用户所有知识点掌握度（薄弱点用） ──
        mastery_all = await KnowledgeMastery.filter(user_id=user_id).all()
        mastery_map: dict[str, dict] = {}
        for m in mastery_all:
            mastery_map[m.knowledge_tag] = {
                "tag": m.knowledge_tag,
                "accuracy": round(m.correct_count / max(m.total_attempts, 1), 2),
                "level": m.mastery_level,
                "total_attempts": m.total_attempts,
            }

        # ── 资源使用数据：收集所有路径的资源 ID ──
        all_resource_ids: set[int] = set()
        path_resource_ids: dict[int, set[int]] = {}
        for p in path_records:
            progress_records = await UserPathProgress.filter(path_id=p.id, user_id=user_id).all()
            ids: set[int] = set()
            for r in progress_records:
                if r.resource_ids:
                    try:
                        ids.update(json.loads(r.resource_ids))
                    except Exception:
                        logger.warning(
                            "路径进度资源 ID 解析失败 user_id=%s path_id=%s node_id=%s raw=%r",
                            user_id, p.id, r.node_id, r.resource_ids,
                            exc_info=True,
                        )
            path_resource_ids[p.id] = ids
            all_resource_ids.update(ids)

        # 批量查询已读状态
        read_statuses = await ResourceReadStatus.filter(
            user_id=user_id,
            resource_id__in=list(all_resource_ids),
        ).all() if all_resource_ids else []
        read_map: dict[int, dict] = {}
        for rs in read_statuses:
            read_map[rs.resource_id] = {"is_read": rs.is_read, "duration_seconds": rs.duration_seconds}

        # 批量查询资源基础数据
        resources = await GeneratedResource.filter(
            id__in=list(all_resource_ids),
            user_id=user_id,
        ).all() if all_resource_ids else []
        res_map: dict[int, dict] = {}
        for r in resources:
            res_map[r.id] = {
                "resource_id": r.id,
                "resource_type": r.resource_type,
                "topic": r.topic,
                "view_count": r.view_count or 0,
                "download_count": r.download_count or 0,
                "last_viewed_at": str(r.last_viewed_at) if r.last_viewed_at else None,
            }

        for p in path_records:
            # ── 进度 ──
            progress_records = await UserPathProgress.filter(path_id=p.id, user_id=user_id).all()
            nodes = sorted(list(p.nodes or []), key=lambda n: n.order_index)
            node_order = {n.id: n.order_index for n in nodes}
            node_map = {n.id: n for n in nodes}
            progress_records = sorted(progress_records, key=lambda r: node_order.get(r.node_id, 10**9))
            total_nodes = len(nodes)
            completed_nodes = sum(1 for r in progress_records if r.node_status == "completed")
            in_progress = sum(1 for r in progress_records if r.node_status == "in_progress")
            unlocked = sum(1 for r in progress_records if r.node_status == "unlocked")

            current_node = None
            for r in progress_records:
                if r.node_status in ("unlocked", "in_progress"):
                    node = node_map.get(r.node_id)
                    current_node = node.topic if node else None
                    break

            # ── 路径薄弱点：匹配该路径节点的 knowledge_tags ──
            path_tags = set()
            for n in nodes:
                if n.knowledge_tags:
                    try:
                        tags = json.loads(n.knowledge_tags)
                        if isinstance(tags, list):
                            path_tags.update(tags)
                    except Exception:
                        logger.warning(
                            "路径节点知识标签解析失败 user_id=%s path_id=%s node_id=%s raw=%r",
                            user_id, p.id, n.id, n.knowledge_tags,
                            exc_info=True,
                        )

            weak_points = []
            for tag in path_tags:
                if tag in mastery_map:
                    m = mastery_map[tag]
                    if m["level"] in ("beginner", "learning"):
                        weak_points.append({**m, "source": "mastery"})
                else:
                    # 路径中存在但未曾练习过的知识点
                    weak_points.append({
                        "tag": tag, "accuracy": 0, "level": "beginner",
                        "total_attempts": 0, "source": "untouched",
                    })

            # 按掌握度排序：beginner 优先
            weak_points.sort(key=lambda w: (0 if w["level"] == "beginner" else 1, w["accuracy"]))

            # ── 路径资源使用情况 ──
            resource_list = []
            read_count = 0
            total_resource_duration = 0
            for rid in path_resource_ids.get(p.id, set()):
                rinfo = res_map.get(rid, {})
                rstatus = read_map.get(rid, {})
                is_read = rstatus.get("is_read", False)
                duration = rstatus.get("duration_seconds", 0)
                if is_read:
                    read_count += 1
                total_resource_duration += duration
                resource_list.append({
                    "resource_id": rid,
                    "resource_type": rinfo.get("resource_type", ""),
                    "topic": rinfo.get("topic", ""),
                    "is_read": is_read,
                    "duration_seconds": duration,
                    "view_count": rinfo.get("view_count", 0),
                    "download_count": rinfo.get("download_count", 0),
                    "last_viewed_at": rinfo.get("last_viewed_at"),
                })
            resource_total = len(resource_list)

            paths_result.append({
                "path_id": p.id,
                "subject": p.subject or "",
                "difficulty": p.difficulty,
                "study_time": {
                    "total_seconds": path_time.get(p.id, 0),
                    "week_seconds": week_path_time.get(p.id, 0),
                    "today_seconds": today_path_time.get(p.id, 0),
                },
                "progress": {
                    "total_nodes": total_nodes,
                    "completed_nodes": completed_nodes,
                    "in_progress_nodes": in_progress,
                    "unlocked_nodes": unlocked,
                    "current_node": current_node,
                    "percentage": round(completed_nodes / max(total_nodes, 1) * 100),
                },
                "resources": {
                    "total": resource_total,
                    "read_count": read_count,
                    "unread_count": resource_total - read_count,
                    "total_duration_seconds": total_resource_duration,
                    "list": resource_list,
                },
                "weak_points": weak_points,
            })

        # 未绑定路径的学习时长汇总
        untracked_time = {
            "total_seconds": path_time.get(0, 0),
            "week_seconds": week_path_time.get(0, 0),
            "today_seconds": today_path_time.get(0, 0),
        }

        # ── 全局汇总 ──
        overall_weak = []
        for m in mastery_all:
            if m.mastery_level in ("beginner", "learning"):
                overall_weak.append({
                    "tag": m.knowledge_tag,
                    "accuracy": round(m.correct_count / max(m.total_attempts, 1), 2),
                    "level": m.mastery_level,
                    "total_attempts": m.total_attempts,
                })

        return {
            "paths": paths_result,
            "untracked_time": untracked_time,
            "total_time": total_time,
            "overall_weak_points": overall_weak,
        }

    @staticmethod
    async def get_exam_weekly(user_id: int) -> dict:
        """最近 7 天每日正确率"""
        today = date.today()
        week_ago = today - timedelta(days=6)

        records = await ExamRecord.filter(
            user_id=user_id,
            is_correct__not_isnull=True,
            created_at__gte=week_ago,
        ).all()

        daily_map: dict[str, dict] = {}
        for r in records:
            day = str(r.created_at.date()) if r.created_at else str(today)
            if day not in daily_map:
                daily_map[day] = {"total": 0, "correct": 0}
            daily_map[day]["total"] += 1
            if r.is_correct:
                daily_map[day]["correct"] += 1

        daily = [
            {
                "date": str(week_ago + timedelta(days=i)),
                "total": daily_map.get(str(week_ago + timedelta(days=i)), {}).get("total", 0),
                "correct": daily_map.get(str(week_ago + timedelta(days=i)), {}).get("correct", 0),
                "accuracy": round(
                    daily_map.get(str(week_ago + timedelta(days=i)), {}).get("correct", 0)
                    / max(daily_map.get(str(week_ago + timedelta(days=i)), {}).get("total", 0), 1),
                    2,
                ),
            }
            for i in range(7)
        ]

        week_total = sum(d["total"] for d in daily)
        week_correct = sum(d["correct"] for d in daily)

        return {
            "daily": daily,
            "week_total": week_total,
            "week_correct": week_correct,
            "week_accuracy": round(week_correct / max(week_total, 1), 2),
        }

    @staticmethod
    async def get_learning_guidance(user_id: int) -> dict:
        """返回个性化学习指导文本"""
        guidance = await build_learning_guidance(user_id)
        return {"guidance": guidance}

    @staticmethod
    async def collect_resource(user_id: int, resource_id: int) -> dict:
        """收藏资源（幂等）"""
        resource = await GeneratedResource.filter(id=resource_id, user_id=user_id).first()
        if not resource:
            raise ValueError("资源不存在")
        await ResourceCollection.get_or_create(user_id=user_id, resource_id=resource_id)
        return {"resource_id": resource_id, "collected": True}

    @staticmethod
    async def uncollect_resource(user_id: int, resource_id: int) -> dict:
        """取消收藏"""
        resource = await GeneratedResource.filter(id=resource_id).first()
        if not resource:
            raise ValueError("资源不存在")
        await ResourceCollection.filter(user_id=user_id, resource_id=resource_id).delete()
        return {"resource_id": resource_id, "collected": False}

    @staticmethod
    async def list_collections(user_id: int) -> list[dict]:
        """列出已收藏资源"""
        collections = await ResourceCollection.filter(user_id=user_id).prefetch_related("resource").order_by("-created_at").all()
        result = []
        for c in collections:
            r = c.resource
            ext_map = {"document": "md", "ppt": "pptx", "mindmap": "txt", "exercise": "md", "case": "md", "reading": "md", "slide_animation": "json", "audio": "mp3", "html": "html"}
            ext = ext_map.get(r.resource_type, "md")
            result.append({
                "resource_id": r.id,
                "topic": r.topic,
                "resource_type": r.resource_type,
                "filename": f"{r.topic}_{r.resource_type}.{ext}",
                "preview": (r.content or "")[:200],
                "download_url": f"/resource/{r.id}/download",
                "collected_at": str(c.created_at),
            })
        return result
