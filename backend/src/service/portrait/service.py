"""画像服务 — 初始化、读取、格式化、置信度计算、再生"""

import json
import logging
import os
import asyncio
import time as _time
from backend.src.models.usermodel import User
from backend.src.models.portraitmodel import User_picture

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


_last_extraction: dict[int, float] = {}

# ═══════════════════════════════════════
#  维度与标签映射（原 portrait_utils）
# ═══════════════════════════════════════

_EXTRACTION_INTERVAL = _env_int("PORTRAIT_EXTRACTION_INTERVAL_SECONDS", 20, minimum=5)

TRAIT_KEYS = [
    "knowbase",
    "commonmis",
    "learning_pace",
    "interest",
    "strengths",
    "weaknesses",
]

LABEL_MAP = {
    "knowbase":      "知识掌握程度(1-5)",
    "commonmis":     "易错点",
    "learning_pace": "学习节奏偏好",
    "interest":      "兴趣方向",
    "strengths":     "学习强项",
    "weaknesses":    "学习弱项",
}

CONFIDENCE_FLOOR = {"popup": 0.75, "user_stated": 0.65, "agent_inferred": 0.30}
CONFIDENCE_CEIL  = {"popup": 0.95, "user_stated": 0.95, "agent_inferred": 0.60}
CONFIDENCE_BOOST = {"popup": 0.08, "user_stated": 0.10, "agent_inferred": 0.10}
CONFIDENCE_MAX = 0.95


def parse_traits(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def dump_traits(traits: dict) -> str:
    return json.dumps(traits, ensure_ascii=False)


def trait_display(traits: dict, key: str) -> str | None:
    data = traits.get(key)
    if data is None:
        return None
    if isinstance(data, dict):
        return data.get("value", "")
    return str(data)


def trait_confident(traits: dict, key: str) -> bool:
    data = traits.get(key)
    if not isinstance(data, dict):
        return False
    return data.get("confidence", 0) >= CONFIDENCE_MAX


def build_trait_entry(value: str, source: str, existing: dict | None = None) -> dict:
    old_conf = existing.get("confidence", 0) if existing else 0
    old_source = existing.get("source", "")    if existing else ""

    floor = CONFIDENCE_FLOOR.get(source, 0.30)
    boost = CONFIDENCE_BOOST.get(source, 0.10)
    ceil  = CONFIDENCE_CEIL.get(source, 0.95)

    if source == old_source:
        new_conf = min(ceil, max(old_conf, floor) + boost)
    else:
        new_conf = min(ceil, max(old_conf, floor))

    return {
        "value": value,
        "confidence": round(new_conf, 2),
        "source": source,
    }


def _unpack(data) -> tuple:
    if isinstance(data, dict):
        return data.get("value"), data.get("confidence", 0)
    return data, 0


def format_portrait(picture, show_missing: bool = False, radar_data: dict | None = None) -> list[str]:
    lines = ["【用户画像】"]

    # 六维雷达（如有）
    if radar_data and radar_data.get("dimensions"):
        lines.append(PortraitRadarService.format_for_prompt(radar_data))

    if picture.cognition:
        lines.append(f"认知风格：{picture.cognition}")
    if picture.learning_goal:
        lines.append(f"学习目标：{picture.learning_goal}")
    if picture.personality_tags:
        try:
            tags = json.loads(picture.personality_tags)
            lines.append(f"性格标签：{'、'.join(tags)}")
        except (json.JSONDecodeError, TypeError):
            lines.append(f"性格标签：{picture.personality_tags}")

    traits = parse_traits(picture.traits)
    filled_keys = []
    for key in TRAIT_KEYS:
        data = traits.get(key)
        if not data:
            continue
        val, conf = _unpack(data)
        if val is None or val == "":
            continue
        filled_keys.append(key)
        label = LABEL_MAP.get(key, key)
        marker = " ✓" if conf >= CONFIDENCE_MAX else ""
        lines.append(f"{label}：{val}（置信度 {conf}）{marker}")

    # 知识点掌握度（给智能体出题/推荐用）
    mastery = traits.get("knowledge_mastery")
    if mastery and isinstance(mastery, list) and len(mastery) > 0:
        lines.append("\n【知识点掌握度】")
        for m in mastery:
            level_cn = {"beginner": "入门", "learning": "学习中", "proficient": "熟练", "mastered": "已掌握"}.get(m.get("level"), m.get("level"))
            lines.append(f"  - {m.get('tag')}：{level_cn}（正确率 {m.get('accuracy', 0)}）")

    if picture.profile_summary:
        lines.append(f"画像总结：{picture.profile_summary}")

    if show_missing:
        missing = [k for k in TRAIT_KEYS if k not in filled_keys]
        if missing:
            lines.append(f"\n【待补充维度】：{'、'.join(missing)}")
        else:
            lines.append("\n【画像状态】：全部维度已完善")

    return lines


def _format_dialogue(dialogue: list[dict]) -> str:
    lines = []
    for i, turn in enumerate(dialogue or [], 1):
        q = str(turn.get("question", "") or "").strip()
        a = str(turn.get("answer", "") or "").strip()
        if q:
            lines.append(f"AI第{i}问：{q}")
        if a:
            lines.append(f"用户回答：{a}")
    return "\n".join(lines)


def _answer_excerpt(dialogue: list[dict], index: int = -1, fallback: str = "这个方向") -> str:
    try:
        answer = str((dialogue or [])[index].get("answer", "") or "").strip()
    except (IndexError, AttributeError):
        answer = ""
    answer = " ".join(answer.split())
    if not answer:
        return fallback
    return answer[:24] + ("..." if len(answer) > 24 else "")


def _fallback_interview_question(step: int, dialogue: list[dict], max_steps: int = 5) -> dict:
    first = _answer_excerpt(dialogue, 0, "你最近想学的内容")
    last = _answer_excerpt(dialogue, -1, "刚才提到的情况")
    questions = [
        "最近你真正想推进的一件事是什么？比如课程、项目、比赛、考试、找工作方向，或者一个想学会的技能。",
        f"围绕「{first}」，你通常更喜欢怎么学？比如看讲解、读资料、动手做、刷题巩固，或者和人讨论。",
        f"学「{first}」时，最容易卡住你的地方是什么？比如概念看不懂、不会迁移、记不住、执行不稳，或者容易分心。",
        "压力大或者任务多的时候，你更像哪种状态？比如越催越冲、需要人拉一把、先乱后稳，或者容易转去做别的。",
        f"以后遇到「{last}」这类问题时，你希望我怎么帮你？比如给路线图、拆每日计划、讲例题、出练习，或者提醒推进。",
    ]
    idx = max(0, min(step, len(questions) - 1))
    return {"question": questions[idx], "finish": step >= max_steps - 1}


# ═══════════════════════════════════════
#  Service 方法
# ═══════════════════════════════════════

class PortraitChatHistory_Service:

    @staticmethod
    async def init_portrait(user_id: int, cognition: str | None,
                            learning_goal: str | None, personality_tags: str | None):
        user = await User.filter(id=user_id).first()
        if not user:
            return None, "未查找到该用户"

        picture = None
        picture_id = getattr(user, "picture_id", None)

        if picture_id:
            picture = await User_picture.filter(id=picture_id).first()

        if not picture:
            try:
                picture = await user.picture
            except Exception:
                picture = None
        if not picture:
            picture = await User_picture.create()
            user.picture = picture
            await user.save()

        if cognition:
            picture.cognition = cognition
        if learning_goal:
            picture.learning_goal = learning_goal
        if personality_tags is not None:
            if isinstance(personality_tags, list):
                picture.personality_tags = json.dumps(personality_tags, ensure_ascii=False)
            else:
                picture.personality_tags = personality_tags

        await picture.save()
        return user, "画像初始化成功"

    @staticmethod
    async def read_portrait(user_id: int):
        user = await User.filter(id=user_id).first()
        if not user:
            return None, "未查找到该用户"

        picture = await user.picture
        if not picture:
            return None, "该用户暂无画像"

        data = {
            "cognition": picture.cognition,
            "learning_goal": picture.learning_goal,
            "personality_tags": picture.personality_tags,
            "traits": parse_traits(picture.traits),
            "profile_summary": picture.profile_summary,
        }
        return data, "获取画像成功"

    @staticmethod
    async def regenerate_portrait(user_id: int) -> dict:
        """调用 LLM 生成画像自然语言摘要，并推断认知风格/学习目标"""
        user = await User.filter(id=user_id).first()
        if not user:
            raise ValueError("用户不存在")
        picture = await user.picture
        if not picture:
            raise ValueError("画像不存在，请先初始化")

        portrait_lines = format_portrait(picture, show_missing=True)
        portrait_text = "\n".join(portrait_lines)

        from backend.src.ai_core.llm_config import llm

        prompt = f"""你是一个学习画像分析师。根据以下用户画像数据，完成两件事：

1. 用一段流畅的中文（80-150字）总结该学习者的整体情况，包括知识水平、学习特点、待提升方向。
2. 推断最可能的学习目标(learning_goal)：exam/competition/certification/interest/job 之一。
3. 推断最可能的认知风格(cognition)：visual/auditory/read-write/practical 之一。

{portrait_text}

请严格按JSON格式输出，不要加任何额外文字：
{{"profile_summary": "...", "learning_goal": "...", "cognition": "..."}}"""

        try:
            response = await llm.ainvoke(prompt)
            raw = response.content.strip()
            from backend.src.utils.json_parser import parse_llm_json
            result = parse_llm_json(raw)
        except Exception:
            logger.exception("LLM 画像摘要生成失败 user_id=%s", user_id)
            raise RuntimeError("画像摘要生成失败，请稍后重试")

        summary = result.get("profile_summary", "")
        learning_goal = result.get("learning_goal", "")
        cognition = result.get("cognition", "")

        if summary:
            picture.profile_summary = summary
        if learning_goal and not picture.learning_goal:
            picture.learning_goal = learning_goal
        if cognition and not picture.cognition:
            picture.cognition = cognition
        await picture.save()

        data = {
            "cognition": picture.cognition,
            "learning_goal": picture.learning_goal,
            "personality_tags": picture.personality_tags,
            "traits": parse_traits(picture.traits),
            "profile_summary": picture.profile_summary,
        }
        return data


    @staticmethod
    async def next_interview_question(
        user_id: int,
        dialogue: list[dict],
        step: int = 0,
        max_steps: int = 5,
    ) -> dict:
        """Generate the next student-profile onboarding question."""
        step = max(0, int(step or 0))
        max_steps = max(3, min(int(max_steps or 5), 5))
        if step >= max_steps:
            return {"question": "", "finish": True}

        dialogue_text = _format_dialogue(dialogue)
        if not dialogue_text and step == 0:
            return _fallback_interview_question(step, dialogue, max_steps)

        try:
            from backend.src.ai_core.llm_config import llm
            from backend.src.utils.prompt_loader import load_prompt, fill_prompt
            from backend.src.utils.json_parser import parse_llm_json

            template = load_prompt("portrait/interview_next")
            prompt = fill_prompt(
                template,
                step=str(step + 1),
                max_steps=str(max_steps),
                dialogue_text=dialogue_text or "暂无，准备提出第一问",
            )
            response = await asyncio.wait_for(
                llm.ainvoke(prompt, priority="low", user_id=int(user_id), pool="portrait"),
                timeout=10,
            )
            result = parse_llm_json(response.content.strip())
            question = str(result.get("question", "") if isinstance(result, dict) else "").strip()
            finish = bool(result.get("finish", False)) if isinstance(result, dict) else False
            if question and 8 <= len(question) <= 90:
                return {"question": question, "finish": finish or step >= max_steps - 1}
        except Exception:
            logger.debug("画像访谈下一问生成失败，使用兜底问题 user_id=%s step=%s", user_id, step, exc_info=True)

        return _fallback_interview_question(step, dialogue, max_steps)

    @staticmethod
    async def init_from_dialogue(user_id: int, dialogue: list[dict]) -> dict:
        """通过多轮问答对话让 LLM 提取并初始化用户画像"""
        user = await User.filter(id=user_id).first()
        if not user:
            raise ValueError("用户不存在")

        picture = await user.picture
        if not picture:
            picture = await User_picture.create()
            user.picture = picture
            await user.save()

        # 格式化对话文本
        lines = []
        for i, turn in enumerate(dialogue):
            q = turn.get("question", "").strip()
            a = turn.get("answer", "").strip()
            if q:
                lines.append(f"AI 第{i+1}问：{q}")
            if a:
                lines.append(f"用户回答：{a}")
        dialogue_text = "\n".join(lines)

        if not dialogue_text.strip():
            raise ValueError("对话内容为空，无法提取画像")

        from backend.src.ai_core.llm_config import llm
        from backend.src.utils.prompt_loader import load_prompt, fill_prompt
        from backend.src.utils.json_parser import parse_llm_json

        template = load_prompt("portrait/init_from_dialogue")
        prompt = fill_prompt(template, dialogue_text=dialogue_text)

        try:
            response = await llm.ainvoke(prompt)
            result = parse_llm_json(response.content.strip())
        except Exception:
            logger.exception("对话画像提取 LLM 调用失败 user_id=%s", user_id)
            raise RuntimeError("画像分析失败，请稍后重试")

        if not result or not isinstance(result, dict):
            logger.warning("对话画像提取无有效返回 user_id=%s", user_id)
            raise RuntimeError("画像分析无结果")

        cognition = result.get("cognition", "") or ""
        learning_goal = result.get("learning_goal", "") or ""
        tags = result.get("personality_tags") or []
        profile_summary = str(result.get("profile_summary", "") or "").strip()
        extracted_traits = result.get("traits") if isinstance(result.get("traits"), dict) else {}

        # 只写非空值
        if cognition:
            picture.cognition = cognition
        if learning_goal:
            picture.learning_goal = learning_goal
        if isinstance(tags, list) and tags:
            import json
            picture.personality_tags = json.dumps(tags, ensure_ascii=False)
        if profile_summary:
            picture.profile_summary = profile_summary[:240]

        # 同步写入 traits 的 interest 维度（如果有对话提取的兴趣信息）
        traits = parse_traits(picture.traits)
        if tags:
            traits["interest"] = build_trait_entry(
                "、".join(tags[:3]), "user_stated", traits.get("interest")
            )

            # 从标签中推断 strengths
            ability_keywords = ["逻辑", "分析", "表达", "动手", "创意", "记忆", "专注", "思维"]
            strength_tags = [t for t in tags if any(kw in t for kw in ability_keywords)]
            if strength_tags:
                traits["strengths"] = build_trait_entry(
                    "、".join(strength_tags), "user_stated", traits.get("strengths")
                )

        for key in TRAIT_KEYS:
            value = extracted_traits.get(key) or result.get(key)
            if isinstance(value, list):
                value = "、".join(str(item).strip() for item in value if str(item).strip())
            value = str(value or "").strip()
            if value:
                traits[key] = build_trait_entry(value[:120], "user_stated", traits.get(key))

        picture.traits = dump_traits(traits)
        await picture.save()
        try:
            from backend.src.service.chat.service import invalidate_portrait_cache
            invalidate_portrait_cache(user_id)
        except Exception:
            logger.debug("画像缓存失效失败 user_id=%s", user_id, exc_info=True)

        logger.info("对话画像初始化成功 user_id=%s cognition=%s goal=%s tags=%s",
                     user_id, cognition, learning_goal, tags)

        return {
            "cognition": picture.cognition,
            "learning_goal": picture.learning_goal,
            "personality_tags": picture.personality_tags,
            "traits": parse_traits(picture.traits),
            "profile_summary": picture.profile_summary,
        }


# ═══════════════════════════════════════
#  六维雷达 Service
# ═══════════════════════════════════════

RADAR_DIMENSIONS = [
    {"key": "memory",        "label": "记忆",   "desc": "easy 难度题目正确率"},
    {"key": "understanding", "label": "理解",   "desc": "medium 难度题目正确率"},
    {"key": "application",   "label": "应用",   "desc": "hard 难度题目正确率"},
    {"key": "analysis",      "label": "分析",   "desc": "多选题正确率"},
    {"key": "breadth",       "label": "广度",   "desc": "已覆盖知识标签种类数"},
    {"key": "persistence",   "label": "坚持",   "desc": "近 30 天活跃天数占比"},
]


class PortraitRadarService:

    @staticmethod
    async def compute(user_id: int) -> dict:
        """从答题数据实时计算六维雷达分数并写入 PortraitRadar 表"""
        from datetime import datetime, timedelta
        from backend.src.models.exam_model import ExamRecord, ExamQuestion, KnowledgeMastery
        from backend.src.models.portrait_radar_model import PortraitRadar

        user = await User.filter(id=user_id).first()
        if not user:
            raise ValueError("用户不存在")

        # 所有已判分的答题记录（join 题目表拿 difficulty/question_type）
        records = await ExamRecord.filter(
            user_id=user_id, is_correct__not_isnull=True
        ).prefetch_related("question").all()

        def _accuracy(questions) -> int:
            """给定题目列表，算正确率百分比整数"""
            if not questions:
                return 0
            scored = [(r.is_correct, r.question) for r in records if r.question in questions]
            if not scored:
                return 0
            correct = sum(1 for ok, _ in scored if ok)
            return round(correct / len(scored) * 100)

        # 按难度分组
        easy_qs = [r.question for r in records if r.question and r.question.difficulty == "easy"]
        medium_qs = [r.question for r in records if r.question and r.question.difficulty == "medium"]
        hard_qs = [r.question for r in records if r.question and r.question.difficulty == "hard"]

        memory = _accuracy(easy_qs)
        understanding = _accuracy(medium_qs)
        application = _accuracy(hard_qs)

        # 分析 — multi_choice 题型正确率
        multi_qs = [r.question for r in records if r.question and r.question.question_type == "multi_choice"]
        analysis = _accuracy(multi_qs)

        # 广度 — 已覆盖知识标签种类数（50 种 = 100 分）
        mastery_records = await KnowledgeMastery.filter(user_id=user_id).all()
        tag_count = len(mastery_records)
        breadth = min(100, round(tag_count / 50 * 100))

        # 坚持 — 近 30 天活跃天数占比
        from datetime import timezone as tz
        cutoff = datetime.now(tz.utc) - timedelta(days=30)
        recent_dates = set()
        for r in records:
            ct = r.created_at
            if not ct:
                continue
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=tz.utc)
            if ct >= cutoff:
                recent_dates.add(ct.date())
        persistence = min(100, round(len(recent_dates) / 30 * 100))

        # 写入/更新 Radar 表
        radar, _ = await PortraitRadar.get_or_create(user=user)
        radar.memory = memory
        radar.understanding = understanding
        radar.application = application
        radar.analysis = analysis
        radar.breadth = breadth
        radar.persistence = persistence
        await radar.save()

        return PortraitRadarService._format(radar)

    @staticmethod
    async def get(user_id: int) -> dict | None:
        """获取最新雷达数据，不存在则自动计算"""
        from backend.src.models.portrait_radar_model import PortraitRadar

        radar = await PortraitRadar.filter(user_id=user_id).first()
        try:
            return await PortraitRadarService.compute(user_id)
        except ValueError:
            return PortraitRadarService._format(radar) if radar else None
        except Exception:
            logger.exception("portrait radar recompute failed user_id=%s", user_id)
            return PortraitRadarService._format(radar) if radar else None

    @staticmethod
    def _format(radar) -> dict:
        return {
            "radar_id": radar.id,
            "user_id": radar.user_id,
            "dimensions": [
                {"key": "memory",        "label": "记忆",   "score": radar.memory,        "desc": "简单题正确率"},
                {"key": "understanding", "label": "理解",   "score": radar.understanding, "desc": "中等题正确率"},
                {"key": "application",   "label": "应用",   "score": radar.application,   "desc": "困难题正确率"},
                {"key": "analysis",      "label": "分析",   "score": radar.analysis,      "desc": "多选题正确率"},
                {"key": "breadth",       "label": "广度",   "score": radar.breadth,       "desc": "知识标签覆盖度"},
                {"key": "persistence",   "label": "坚持",   "score": radar.persistence,   "desc": "近30天活跃度"},
            ],
            "updated_at": str(radar.updated_at),
        }

    @staticmethod
    def format_for_prompt(radar: dict) -> str:
        """格式化为 agent prompt 可用的文本"""
        if not radar or not radar.get("dimensions"):
            return "暂无六维雷达数据"
        lines = ["【六维能力雷达】"]
        for d in radar["dimensions"]:
            bar = "█" * (d["score"] // 10) + "░" * (10 - d["score"] // 10)
            lines.append(f"  {d['label']}：{bar} {d['score']}")
        return "\n".join(lines)

    @staticmethod
    async def sync_to_portrait(user_id: int) -> None:
        """雷达分数反哺画像 traits（strengths/weaknesses）"""
        from backend.src.models.portrait_radar_model import PortraitRadar

        radar = await PortraitRadar.filter(user_id=user_id).first()
        if not radar:
            return

        user = await User.filter(id=user_id).first()
        if not user:
            return
        picture = await user.picture
        if not picture:
            picture = await User_picture.create()
            user.picture = picture
            await user.save()

        traits = parse_traits(picture.traits)
        scores = {
            "记忆": radar.memory, "理解": radar.understanding, "应用": radar.application,
            "分析": radar.analysis, "广度": radar.breadth, "坚持": radar.persistence,
        }

        strengths = [f"{k}({v})" for k, v in scores.items() if v >= 70]
        weaknesses = [f"{k}({v})" for k, v in scores.items() if v < 50]

        if strengths:
            traits["strengths"] = build_trait_entry(
                "、".join(strengths), "agent_inferred", traits.get("strengths")
            )
        if weaknesses:
            traits["weaknesses"] = build_trait_entry(
                "、".join(weaknesses), "agent_inferred", traits.get("weaknesses")
            )

        picture.traits = dump_traits(traits)
        await picture.save()


async def build_learning_guidance(user_id: int) -> str:
    """读雷达+画像+掌握度，输出显式的 LLM 学习指导文本"""
    try:
        radar_data = await PortraitRadarService.get(user_id)
    except Exception:
        return ""
    if not radar_data or not radar_data.get("dimensions"):
        return ""

    dims = {d["key"]: d["score"] for d in radar_data["dimensions"]}
    levels = {}
    for key, score in dims.items():
        if score >= 80:
            levels[key] = "优秀"
        elif score >= 60:
            levels[key] = "尚可"
        elif score >= 40:
            levels[key] = "薄弱，需重点训练"
        else:
            levels[key] = "严重不足"

    labels = {"memory": "记忆(简单题)", "understanding": "理解(中等题)", "application": "应用(困难题)",
              "analysis": "分析(多选题)", "breadth": "广度(知识覆盖)", "persistence": "坚持(活跃度)"}

    lines = []
    # 一、能力分析
    lines.append("## 学习者能力分析")
    for key, label in labels.items():
        score = dims.get(key, 0)
        level = levels.get(key, "未知")
        lines.append(f"- {label}: {score}分 — {level}")

    # 二、学习策略建议
    lines.append("\n## 学习策略建议")
    weak = [(k, v) for k, v in dims.items() if v < 50]
    moderate = [(k, v) for k, v in dims.items() if 50 <= v < 70]
    strong = [(k, v) for k, v in dims.items() if v >= 70]
    if weak:
        w_labels = [labels.get(k, k) for k, _ in weak]
        lines.append(f"- 薄弱环节：{'、'.join(w_labels)} → 优先分配学习资源")
    if moderate:
        m_labels = [labels.get(k, k) for k, _ in moderate]
        lines.append(f"- 巩固方向：{'、'.join(m_labels)} → 保持练习频率")
    if strong:
        s_labels = [labels.get(k, k) for k, _ in strong]
        lines.append(f"- 优势利用：{'、'.join(s_labels)} → 可作为学习加速器")

    # 三、出题指导
    lines.append("\n## 出题指导")
    mem, und, app = dims.get("memory", 50), dims.get("understanding", 50), dims.get("application", 50)
    total_gap = max((100 - mem) + (100 - und) + (100 - app), 1)
    easy_pct = max(15, round((100 - mem) / total_gap * 100))
    medium_pct = round((100 - und) / total_gap * 100)
    hard_pct = min(50, 100 - easy_pct - medium_pct)
    medium_pct = 100 - easy_pct - hard_pct  # 确保加起来 = 100
    lines.append(f"- 建议难度配比：easy {easy_pct}% / medium {medium_pct}% / hard {hard_pct}%")
    if dims.get("analysis", 50) < 60:
        lines.append("- 分析能力不足：增加多选题比例")
    if dims.get("application", 50) < 50:
        lines.append("- 应用能力薄弱：每道困难题附带详细解析")

    # 弱项知识点
    from backend.src.models.exam_model import KnowledgeMastery
    mastery_records = await KnowledgeMastery.filter(user_id=user_id).all()
    weak_tags = [m.knowledge_tag for m in mastery_records if m.correct_count / max(m.total_attempts, 1) < 0.5]
    if weak_tags:
        lines.append(f"- 弱项知识点优先出题：{'、'.join(weak_tags[:8])}")

    # 四、学习资料指导
    lines.append("\n## 学习资料指导")
    if dims.get("memory", 50) < 60:
        lines.append("- 从基础概念讲起，循序渐进，每节附小结")
    if dims.get("understanding", 50) < 60:
        lines.append("- 增加对比分析和概念辨析内容")
    if dims.get("application", 50) < 50:
        lines.append("- 每节附实践练习和例题讲解")
    if dims.get("analysis", 50) < 60:
        lines.append("- 增加案例分析和归纳总结模块")
    if dims.get("breadth", 50) < 50:
        lines.append("- 引入跨领域连接和拓展阅读")
    if dims.get("persistence", 50) < 30:
        lines.append("- 内容拆分为小块，降低单次学习时长")

    return "\n".join(lines)


async def extract_portrait_from_chat(user_id: int, chat_group_id: int) -> None:
    """每次对话后异步调用，从最近聊天记录中提取画像特征"""
    now = _time.time()
    elapsed = now - _last_extraction.get(user_id, 0)
    if elapsed < _EXTRACTION_INTERVAL:
        logger.debug(f"画像提取冷却中 user_id={user_id} 距上次={elapsed:.0f}s")
        return

    try:
        from backend.src.models.chat_history_model import ChatHistory
        from backend.src.ai_core.llm_config import llm
        from backend.src.utils.prompt_loader import load_prompt, fill_prompt
        from backend.src.utils.json_parser import parse_llm_json

        user = await User.filter(id=user_id).first()
        if not user:
            return

        # 确保画像存在
        picture = await user.picture
        if not picture:
            picture = await User_picture.create()
            user.picture = picture
            await user.save()

        # 取最近 10 条消息
        records = await ChatHistory.filter(
            user_id=user_id, chat_group_id=chat_group_id
        ).order_by("-created_at").limit(10).all()

        if len(records) < 2:
            logger.debug(f"画像提取跳过：消息数不足 user_id={user_id} records={len(records)}")
            return

        _last_extraction[user_id] = now

        messages = []
        for r in reversed(records):
            if r.req:
                messages.append(f"用户：{r.req}")
            if r.res:
                res_short = r.res[:200]
                messages.append(f"AI：{res_short}")

        recent_text = "\n".join(messages)

        # 已有画像
        traits = parse_traits(picture.traits)
        existing = {}
        for key in TRAIT_KEYS:
            entry = traits.get(key)
            if isinstance(entry, dict) and entry.get("confidence", 0) >= 0.95:
                existing[key] = f"{entry['value']}（置信度已满，勿覆盖）"
            elif isinstance(entry, dict) and entry.get("value"):
                existing[key] = entry["value"]
        existing_text = json.dumps(existing, ensure_ascii=False) if existing else "暂无"

        template = load_prompt("portrait/extract")
        prompt = fill_prompt(template, existing_portrait=existing_text, recent_messages=recent_text)

        response = await llm.ainvoke(prompt)
        result = parse_llm_json(response.content.strip())

        if not result or not isinstance(result, dict):
            logger.info(f"画像提取无结果 user_id={user_id} result={str(response.content)[:200]}")
            return

        updated = False
        for key in TRAIT_KEYS:
            value = result.get(key)
            if not value or not isinstance(value, str) or len(value) > 80:
                continue
            entry = traits.get(key) if isinstance(traits.get(key), dict) else None
            if entry and entry.get("confidence", 0) >= 0.95:
                continue
            traits[key] = build_trait_entry(value, "agent_inferred", entry)
            updated = True

        if updated:
            picture.traits = dump_traits(traits)
            await picture.save()
            logger.info(f"画像提取成功 user_id={user_id} 更新维度={[k for k in result if k in TRAIT_KEYS]}")

            # 自动刷新画像总结
            try:
                await PortraitChatHistory_Service.regenerate_portrait(user_id)
            except Exception:
                logger.exception(f"画像总结刷新失败 user_id={user_id}")

    except Exception:
        logger.exception(f"画像提取失败 user_id={user_id}")
