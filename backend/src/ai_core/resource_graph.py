"""
LangGraph 多智能体编排 — 学习资源生成
LeaderAgent → [ExecutorAgent × N 线程并行] → ReviewerAgent
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
from typing import TypedDict, NotRequired

from langgraph.graph import StateGraph, START, END

from backend.src.ai_core.llm_config import llm, llm_vision
from backend.src.ai_core.ppt_planner import (
    DOC_DEFAULT_SECTIONS,
    DOC_SECTION_COUNT_BY_DEPTH,
    PPT_DEFAULT_SECTIONS,
    estimate_ppt_section_count,
    generate_formula_sheet,
    generate_learning_objectives,
    generate_ppt_outline,
)
from backend.src.ai_core.streaming import (
    push_agent_event as _push_agent_event,
    push_text_stream as _push_text_stream,
    safe_stream_writer as _safe_stream_writer,
)
from backend.src.utils.formula_builder import build_formula_sheet
from backend.src.utils.knowledge_base import search as kb_search
from backend.src.utils.prompt_loader import load_prompt, fill_prompt
from backend.src.utils.json_parser import parse_llm_json

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


PPT_TARGET_CONCURRENT_USERS = max(1, _int_env("PPT_TARGET_CONCURRENT_USERS", 5))
PPT_MAX_SECTIONS_PER_REQUEST = max(1, _int_env("PPT_MAX_SECTIONS_PER_REQUEST", 19))
_PPT_GLOBAL_GEN_SEM = asyncio.Semaphore(PPT_TARGET_CONCURRENT_USERS * PPT_MAX_SECTIONS_PER_REQUEST)

# 资源类型 → 默认 prompt 路径
PROMPT_MAP = {
    "document": "resource/document",
    "ppt": "resource/ppt",
    "mindmap": "resource/mindmap",
    "exercise": "resource/exam",
    "case": "resource/document",
    "reading": "resource/document",
    "image": "resource/image_prompt",
}

# ═══════════════════════════════════════
#  State
# ═══════════════════════════════════════

class ResourceState(TypedDict):
    user_id: str
    topic: str
    resource_types: list[str]
    chat_group_id: NotRequired[int]
    portrait_context: str
    kb_context: str
    learning_guidance: str
    user_notes: str
    custom_prompts: dict
    generated_resources: dict
    review_feedback: str
    review_passed: bool
    retry_count: int
    exam_question_types: str
    exam_count: str
    exam_difficulty: str
    reviewer_questions: list[dict]
    file_urls: NotRequired[dict[str, str]]
    answers: NotRequired[dict]
    skip_review: NotRequired[bool]
    ppt_prompt_key: NotRequired[str]
    llm_priority: NotRequired[str]
    ppt_theme_id: NotRequired[str]
    rag_mode: NotRequired[str]


# ═══════════════════════════════════════
#  Nodes
# ═══════════════════════════════════════

async def leader_node(state: ResourceState) -> dict:
    """LeaderAgent: 分析需求，决定生成哪些资源类型（用户已指定则跳过 LLM）"""
    writer = _safe_stream_writer()
    _push_agent_event(writer, "leader", "LeaderAgent", "leader", "running", "正在分析学习需求")
    requested = state.get("resource_types") or []
    if requested:
        _push_agent_event(
            writer,
            "leader",
            "LeaderAgent",
            "leader",
            "done",
            f"已确认生成类型：{' / '.join(requested)}",
            total=len(requested),
        )
        return {"resource_types": requested}

    topic = state["topic"]
    portrait = state.get("portrait_context", "")
    kb = state.get("kb_context", "")
    guidance = state.get("learning_guidance", "")
    prompt_text = fill_prompt(load_prompt("agent/leader"), topic=topic, portrait_context=portrait, kb_context=kb, learning_guidance=guidance)

    try:
        response = await llm.ainvoke(prompt_text, priority=state.get("llm_priority", "high"), user_id=int(state.get("user_id", 0)), pool="leader")
    except Exception as e:
        logger.exception("LeaderAgent LLM 调用失败")
        _push_agent_event(writer, "leader", "LeaderAgent", "leader", "failed", "规划失败，降级生成文档")
        return {"resource_types": ["document"]}

    try:
        plan = parse_llm_json(response.content)
    except json.JSONDecodeError:
        plan = {"resource_types": ["document"], "topic": topic, "outline": response.content.strip()}

    resource_types = plan.get("resource_types", ["document"])
    _push_agent_event(
        writer,
        "leader",
        "LeaderAgent",
        "leader",
        "done",
        f"规划完成：{' / '.join(resource_types)}",
        total=len(resource_types),
    )
    return {"resource_types": resource_types}


_template_cache: dict[str, str] = {}


def _normalize_rag_mode(mode: str | None) -> str:
    return "strict" if str(mode or "").strip().lower() in {"strict", "source_only", "knowledge_only"} else "reference"


def _format_kb_context(kb: str = "", rag_mode: str = "reference") -> str:
    clean_kb = str(kb or "").strip()
    if not clean_kb or "暂无" in clean_kb or "No knowledge" in clean_kb:
        clean_kb = "暂无可靠知识库资料。"

    if _normalize_rag_mode(rag_mode) == "strict":
        policy = (
            "【知识库使用模式：严格资料模式】\n"
            "- 只根据下方知识库资料组织内容。\n"
            "- 下方资料没有覆盖的知识点，不要自行扩写为确定结论。\n"
            "- 资料不足时，请明确写成学习缺口或待补充资料，不要硬凑内容。\n"
        )
    else:
        policy = (
            "【知识库使用模式：智能参考模式】\n"
            "- 下方知识库资料只作为优先参考，不是唯一依据。\n"
            "- 如果资料覆盖不足，可以使用通用学科知识补全教学链路，但不要声称这些补充内容来自知识库。\n"
            "- 如果资料与通用学科知识冲突或明显不相关，优先采用更稳妥的通用解释，并避免强行引用。\n"
            "- 不需要在每页强制标注来源；只在确实采用资料中的具体表述、例子或数据时自然说明来源。\n"
        )
    return f"{policy}\n【知识库参考资料】\n{clean_kb}"

def build_resource_prompt(
    rt: str, topic: str, portrait: str = "", kb: str = "",
    guidance: str = "", feedback: str = "", user_notes: str = "",
    exam_count: str = "5", question_types: str = "single_choice, multi_choice, true_false",
    difficulty: str = "medium", custom_prompts: dict | None = None,
    focus_guidance: str = "",
    section: str = "",
    learning_objectives: str = "",
    formula_sheet: str = "",
    ppt_theme_id: str = "",
    rag_mode: str = "reference",
) -> str:
    """构建单个资源类型的生成 prompt，可从 executor_node 或 generate_stream 直接调用"""
    custom_prompts = custom_prompts or {}
    custom = custom_prompts.get(rt, "")
    if custom.strip():
        template = custom
    else:
        prompt_path = PROMPT_MAP.get(rt, "resource/document")
        if prompt_path not in _template_cache:
            _template_cache[prompt_path] = load_prompt(prompt_path)
        template = _template_cache[prompt_path]
    kb_limit = _int_env("RAG_PROMPT_CONTEXT_CHARS", 2500)
    rt_kb = kb[:kb_limit] if len(kb) > kb_limit else kb  # 截断，公式已由 formula_sheet 覆盖
    rt_kb = _format_kb_context(rt_kb, rag_mode)
    base = fill_prompt(
        template,
        topic=topic,
        resource_type=rt,
        portrait_context=portrait,
        kb_context=rt_kb,
        learning_guidance=guidance,
        user_notes=user_notes,
        feedback=feedback,
        count=exam_count,
        question_types=question_types,
        difficulty=difficulty,
        section=section,
        learning_objectives=learning_objectives or "暂无学习目标数据",
        formula_sheet=formula_sheet,
        ppt_theme_id=ppt_theme_id or "",
    )
    return base + focus_guidance if focus_guidance else base


async def generate_ppt_parallel(
    topic: str,
    portrait: str = "",
    kb: str = "",
    guidance: str = "",
    feedback: str = "",
    user_notes: str = "",
    custom_prompts: dict | None = None,
    sections: list[str] | None = None,
    stream_writer=None,
    section_count: int = PPT_DEFAULT_SECTIONS,
    ppt_prompt_key: str = "ppt",
    llm_priority: str = "high",
    user_id: int = 0,
    learning_objectives: str = "",
    skip_review_sections: bool = False,
    ppt_theme_id: str = "",
    rag_mode: str = "reference",
) -> str:
    """按章节并行生成 PPT：大纲（默认{section_count}章节） → N 条线并行（每条 2-5 页），共 2N-5N 页 + 2 页画像学习引入"""
    _t_total = time.perf_counter()
    _push_agent_event(stream_writer, "executor:ppt", "PPT生成智能体", "executor", "running", "正在启动 PPT 生成", resource_type="ppt")
    # 立即通知前端，避免长时间无反馈
    if stream_writer:
        try:
            stream_writer({"type": "stream_start", "file_type": "ppt"})
        except Exception:
            logger.exception("[PPT-Parallel] stream_start 推送异常")

    section_guidance_map: dict[str, str] = {}
    course_plan_text = ""

    if sections is None:
        if stream_writer:
            try:
                _push_agent_event(stream_writer, "executor:ppt", "PPT生成智能体", "executor", "running", "正在规划课程大纲", resource_type="ppt")
                stream_writer({"type": "stream_progress", "file_type": "ppt", "message": "正在规划课程大纲..."})
            except Exception:
                logger.exception("[PPT-Parallel] stream_progress 推送异常")
        outline_task = generate_ppt_outline(topic, kb=kb, guidance=guidance, count=section_count, llm_priority=llm_priority, user_id=user_id)
        formula_task = generate_formula_sheet(topic, kb=kb, guidance=guidance, llm_priority=llm_priority, user_id=user_id)
        outline_result, formula_sheet = await asyncio.gather(outline_task, formula_task)
        sections, section_guidance_map, course_plan_text = outline_result
        if not learning_objectives:
            learning_objectives = course_plan_text
        _push_agent_event(stream_writer, "executor:ppt", "PPT生成智能体", "executor", "running", f"大纲规划完成，共 {len(sections)} 章", resource_type="ppt", total=len(sections))
    else:
        formula_sheet = ""

    # 画像引入置顶（如果画像数据可用）
    has_portrait = portrait and portrait != "暂无画像数据"
    if has_portrait:
        sections = ["学习引入：从你的视角出发"] + list(sections)

    # 构建课程全景描述，让每个章节知道自己在哪里
    outline_lines = [f"第{i+1}章「{s}」" for i, s in enumerate(sections)]
    course_overview = "\n".join(outline_lines)

    def _strip_framework(text: str) -> str:
        """剥离所有 markdown 框架标记，只保留正文文字"""
        t = text
        t = re.sub(r'\$\$[\s\S]*?\$\$', '', t)          # 块公式
        t = re.sub(r'\$[^$]*\$', '', t)                  # 行内公式
        t = re.sub(r'```[\s\S]*?```', '', t)             # 代码块
        t = re.sub(r'<!--[^>]*-->', '', t)               # HTML 注释（含 layout）
        t = re.sub(r'^#{1,4}\s+', '', t, flags=re.MULTILINE)  # 标题 #/##/###/####
        t = re.sub(r'^[-*+]\s+', '', t, flags=re.MULTILINE)   # 无序列表
        t = re.sub(r'^\d+[.)]\s+', '', t, flags=re.MULTILINE) # 有序列表
        t = re.sub(r'\*\*([^*]+)\*\*', r'\1', t)         # 加粗
        t = re.sub(r'\*([^*]+)\*', r'\1', t)             # 斜体
        t = re.sub(r'^>\s*', '', t, flags=re.MULTILINE)  # 引用
        t = re.sub(r'\|', ' ', t)                        # 表格竖线
        t = re.sub(r'^[-*_]{3,}\s*$', '', t, flags=re.MULTILINE)  # 分隔线
        t = re.sub(r'!?\[([^\]]*)\]\([^)]+\)', r'\1', t) # 链接/图片
        t = re.sub(r'`([^`]+)`', r'\1', t)               # 行内代码
        return t.strip()

    def _quick_check_ppt(content: str) -> tuple[bool, str]:
        """格式安全快检：只拦截机械硬伤，内容质量留给 reviewer。"""
        normalized = str(content or "").strip()
        normalized = re.sub(r"^```(?:markdown|md)?\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*```$", "", normalized)
        if not normalized:
            return False, "empty_content"
        if re.search(r"(生成失败|无法生成|生成出错|failed to generate|generation failed)", normalized[:800], re.IGNORECASE):
            return False, "generation_failure_marker"

        hollow_labels = (
            "学习目标", "核心规则", "最小例子", "自查提醒", "承接目标", "操作示范",
            "条件边界", "迁移检查", "要点", "步骤", "第一步", "第二步", "第三步",
            "第四步", "第五步",
        )

        def _content_len(text: str) -> int:
            clean = re.sub(r"`([^`]+)`", r"\1", text)
            clean = re.sub(r"\$[^$]*\$", "", clean)
            return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", clean))

        def _is_hollow_item(text: str) -> bool:
            item = re.sub(r"^\s*(?:[-*+]|\d+[.)、])\s*", "", text).strip()
            item = re.sub(r"^\s*(?:\*\*)?(.+?)(?:\*\*)?\s*$", r"\1", item).strip()
            match = re.match(r"^([^：:]{1,12})[：:]\s*(.*)$", item)
            if not match:
                return _content_len(item) < 8
            label, body = match.group(1).strip(), match.group(2).strip()
            if any(label.startswith(name) or name in label for name in hollow_labels):
                return _content_len(body) < 12
            return _content_len(item) < 12

        slides = re.split(r"\n\s*---+\s*\n", normalized)
        checked_slides = 0
        for slide in slides:
            slide = slide.strip()
            if not slide:
                continue
            if re.search(r'katex|mathml|spanclass|xmlns|semantics|mrow|&lt;/?[a-z]|<(?!/?!--)[a-z][^>]*>', slide, re.IGNORECASE):
                return False, "rendered_html_or_katex_leak"
            if re.search(r'^\s*\$\$\s*\$\$\s*$', slide, re.MULTILINE):
                return False, "empty_formula_block"
            has_layout = slide.startswith("<!-- layout:")
            has_title = slide.startswith("# ") or slide.startswith("## ")
            if not (has_layout or has_title):
                continue
            checked_slides += 1
            if "（上）" in slide or "（下）" in slide:
                return False, "split_title_marker"
            dollars = slide.count("$")
            if dollars % 2 != 0:
                return False, "unbalanced_dollar"
            text_no_math = re.sub(r'\$\$[\s\S]*?\$\$', '', slide)
            text_no_math = re.sub(r'\$[^$]*\$', '', text_no_math)
            if re.search(r'\.{4,}|……{1,}', text_no_math):
                return False, "ellipsis_placeholder"
            if re.search(r'(同上|以此类推|依此类推|类似可得|不再赘述|此处不再展开|（略）|证明略|过程略|推导略|步骤略)', text_no_math):
                return False, "omitted_content"
            if not re.search(r'(?m)^>\s*(?:讲稿|speaker notes?|notes?)\s*[:：]', slide, re.IGNORECASE):
                return False, "missing_speaker_notes"
            visible_lines = []
            for line in text_no_math.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(("<!--", "#", ">")):
                    continue
                if re.match(r"^\s*(?:[-*+]|\d+[.)、])\s*", stripped):
                    if _is_hollow_item(stripped):
                        return False, "hollow_bullet_or_step"
                    visible_lines.append(stripped)
            visible_text = "\n".join(visible_lines)
            if _content_len(visible_text) < 60:
                return False, "too_sparse_visible_content"
        if checked_slides <= 0:
            return False, "missing_slide_title"
        return True, ""

    def _repair_ppt_content(content: str, section_title: str) -> str:
        """修补非致命空槽，降低无意义重试率。"""
        normalized = str(content or "").strip()
        if not normalized:
            return normalized

        def _content_len(text: str) -> int:
            clean = re.sub(r"`([^`]+)`", r"\1", text)
            clean = re.sub(r"\$[^$]*\$", "", clean)
            return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", clean))

        def _repair_line(line: str) -> str:
            match = re.match(r"^(\s*(?:[-*+]|\d+[.)、])\s*)([^：:]{1,12})[：:]\s*(.*)$", line)
            if not match:
                return line
            prefix, label, body = match.group(1), match.group(2).strip(), match.group(3).strip()
            if _content_len(body) >= 8:
                return line
            fallback = (
                f"围绕「{section_title}」补充具体说明：说明本步骤要解决的问题、"
                "使用时需要满足的条件，以及学习者可以立即检查的结果。"
            )
            return f"{prefix}{label}：{fallback}"

        repaired_slides: list[str] = []
        for raw_slide in re.split(r"\n\s*---+\s*\n", normalized):
            slide = raw_slide.strip()
            if not slide:
                continue
            lines = [_repair_line(line) for line in slide.splitlines()]
            repaired = "\n".join(lines).strip()
            notes_match = re.search(r'(?m)^>\s*(?:讲稿|speaker notes?|notes?)\s*[:：].*$', repaired, re.IGNORECASE)
            notes_line = notes_match.group(0) if notes_match else (
                f"> 讲稿：本页围绕「{section_title}」展开讲解。请先说明本页学习目标，"
                "再用一个小例子或检查动作帮助学习者确认自己是否真正理解。"
            )
            if notes_match:
                repaired = (repaired[:notes_match.start()] + repaired[notes_match.end():]).strip()
            visible_text = "\n".join(
                line.strip()
                for line in repaired.splitlines()
                if line.strip() and not line.strip().startswith(("<!--", "#", ">"))
            )
            if _content_len(visible_text) < 60:
                repaired += (
                    f"\n- 学习提示：本页用于承接「{section_title}」的核心任务，"
                    "至少要说清对象、条件、操作和自查标准，避免只停留在标题式概念。"
                )
            repaired = f"{repaired}\n{notes_line}".strip()
            repaired_slides.append(repaired)
        return "\n---\n".join(repaired_slides)

    def _normalize_ppt_content(raw: str, section_title: str) -> str:
        """兼容外部/结构化 PPT 生成器输出，统一转成现有 PPT Markdown。"""
        content = str(raw or "").strip()
        content = re.sub(r"^```(?:markdown|md)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
        if not content:
            return ""

        def _as_list(value):
            return value if isinstance(value, list) else []

        def _text(value) -> str:
            return str(value or "").strip()

        def _coerce_slide(item: dict, index: int) -> dict:
            blocks = item.get("blocks") if isinstance(item.get("blocks"), list) else []
            bullet_source = (
                item.get("bullets")
                or item.get("points")
                or item.get("items")
                or item.get("key_points")
                or []
            )
            bullets: list[str] = []
            for block in list(blocks) + _as_list(bullet_source):
                if isinstance(block, dict):
                    text = _text(block.get("text") or block.get("content") or block.get("value"))
                else:
                    text = _text(block)
                if text:
                    bullets.append(text)

            layout = _text(item.get("layout") or item.get("type"))
            layout_alias = {
                "process": "process_steps",
                "steps": "process_steps",
                "formula": "formula_focus",
                "compare": "comparison",
                "cards": "content_cards",
                "concept": "concept_visual",
            }
            layout = layout_alias.get(layout, layout)

            return {
                "index": index,
                "title": _text(item.get("title") or item.get("heading") or f"{section_title} {index + 1}"),
                "layout": layout,
                "theme": _text(item.get("theme") or ppt_theme_id),
                "bullets": bullets,
                "text": "\n".join(bullets) or _text(item.get("text") or item.get("content")),
                "notes": _text(item.get("notes") or item.get("speaker_notes")),
                "visual": item.get("visual") if isinstance(item.get("visual"), dict) else {},
            }

        if content[:1] in "{[":
            try:
                data = parse_llm_json(content)
                raw_slides = data if isinstance(data, list) else (
                    data.get("slides")
                    or data.get("pages")
                    or (data.get("deck") or {}).get("slides")
                    or []
                )
                slides = [
                    _coerce_slide(item, i)
                    for i, item in enumerate(raw_slides)
                    if isinstance(item, dict)
                ]
                if slides:
                    from backend.src.utils.slide_schema import slides_to_markdown
                    return _repair_ppt_content(slides_to_markdown(section_title, slides).strip(), section_title)
            except Exception:
                logger.debug("[PPT-Gen] structured output normalization failed", exc_info=True)

        first_title = re.search(r"(?m)^\s{0,3}#{1,2}\s+\S+", content)
        if first_title and first_title.start() > 0:
            prefix = content[:first_title.start()].strip()
            if not prefix or re.search(r"^(好的|收到|以下|根据|这里是)", prefix):
                content = content[first_title.start():].strip()
        return _repair_ppt_content(content, section_title)

    def _fallback_ppt_section(section_title: str) -> str:
        safe_title = re.sub(r"[#<>`$]", "", section_title or topic).strip() or "核心知识点"
        return (
            f"## {safe_title}：核心概念梳理\n"
            "<!-- layout: content_cards -->\n"
            f"- 本页用于替代未通过质量审核的初稿，先围绕「{safe_title}」建立稳定的概念框架，避免错误公式、残缺推导或渲染标签进入学习材料。\n"
            f"- 学习时先确认本章节讨论的对象、条件和目标，再进入具体例子；这样可以把「{safe_title}」放回完整知识链条中理解。\n"
            "- 对公式密集内容，优先用小规模例子验证含义，再推广到一般情形；每一步都应说明变量来源、运算规则和适用前提。\n"
            "- 如果后续需要更精细的推导，可以重新生成本章节或改用文档资源承载长公式，避免在幻灯片里塞入过长表达式。\n"
            f"> 讲稿：本页先把「{safe_title}」放回完整学习链条中讲清楚。学习者需要先确认对象、条件和目标，再用一个最小例子验证概念含义；如果涉及公式，应优先检查变量来源和适用前提，而不是直接套用结论。\n"
            "\n---\n"
            f"## {safe_title}：应用检查清单\n"
            "<!-- layout: process_steps -->\n"
            "- 第一步：用一句话说清本章节概念解决什么问题，并列出至少两个关键词，检查自己是否只记住了符号而没有理解意义。\n"
            "- 第二步：选择一个最小例子进行手算或口头推演，重点观察每一步为什么成立，而不是直接跳到最后答案。\n"
            "- 第三步：对比常见错误做法，尤其关注符号位置、维度匹配、条件遗漏和把结论套用到不适用场景的问题。\n"
            "- 第四步：完成一道同类型练习后复述解题路径，确认能从题目信息推出所用方法，而不是依赖题目标题提示。\n"
            f"> 讲稿：这一页用于把「{safe_title}」转化成可执行的学习动作。不要只看自己能否记住定义，而要看能否说出使用条件、完成一个最小例子，并解释错误做法为什么不成立；能复述路径，才算真正掌握。\n"
        )


    def _trim_section_context(
        full_portrait: str,
        full_lo: str,
        full_guidance: str,
        section_title: str,
        idx: int,
        total_sections: int,
    ) -> tuple[str, str, str]:
        """根据章节动态裁剪画像/学习目标/指导语，减少重复 token 注入

        首章（含引入页）返回完整内容，后续章节只保留章节定位等精简信息。
        非首章每章可节省约 1100 字 input token。

        Returns:
            (trimmed_portrait, trimmed_lo, trimmed_guidance)
        """
        if idx == 0:
            return full_portrait, full_lo, full_guidance

        # 后续章节：画像压缩为一行定位提示
        trimmed_portrait = (
            f"（用户画像要点见首章引入页。当前第{idx + 1}章「{section_title}」，请参考前文画像信息调整难度与侧重点。）"
            if full_portrait and full_portrait != "暂无画像数据"
            else ""
        )

        # 学习目标压缩为章节定位
        trimmed_lo = (
            f"本课程共{total_sections}章，当前第{idx + 1}章「{section_title}」。"
            f"整体学习目标见首章引入页，本节聚焦该章节核心知识点展开。"
            if full_lo and full_lo != "暂无学习目标数据"
            else ""
        )

        # 指导语截取核心要求（保留前120字）
        if full_guidance:
            trimmed_guidance = full_guidance[:120]
            if len(full_guidance) > 120:
                trimmed_guidance += "…（完整要求见首章）"
        else:
            trimmed_guidance = ""

        return trimmed_portrait, trimmed_lo, trimmed_guidance







    total = len(sections)
    _results: list[dict] = [{} for _ in range(total)]
    gen_sem = asyncio.Semaphore(max(1, total))
    review_sem = asyncio.Semaphore(2)
    soft_quick_reasons = {"missing_speaker_notes", "hollow_bullet_or_step", "too_sparse_visible_content"}
    first_pass_done = [False] * total
    first_pass_event = asyncio.Event()

    def _mark_first_pass_done(_idx: int):
        if 0 <= _idx < total and not first_pass_done[_idx]:
            first_pass_done[_idx] = True
            if all(first_pass_done):
                first_pass_event.set()

    def _slide_stream_meta(_idx: int, slide_idx: int, _section_title: str) -> dict:
        return {
            "file_type": "ppt",
            "section_idx": _idx,
            "slide_idx": slide_idx,
            "section_title": _section_title,
            "section_total": total,
        }

    def _iter_text_chunks(text: str, chunk_size: int = 12):
        for start in range(0, len(text), chunk_size):
            yield text[start:start + chunk_size]

    def _push_section(_idx: int, _content: str, _section_title: str = ""):
        """将章节的幻灯片逐页推送给前端"""
        if stream_writer:
            try:
                for slide_idx, slide in enumerate(_content.split("\n---\n")):
                    slide = slide.strip()
                    if slide:
                        if slide.startswith("<!-- layout:"):
                            slide = slide.replace("\n# ", "\n## ", 1)
                        elif slide.startswith("# ") and not slide.startswith("## "):
                            slide = slide.replace("# ", "## ", 1)
                        meta = _slide_stream_meta(_idx, slide_idx, _section_title)
                        stream_writer({
                            "type": "stream_slide_start",
                            **meta,
                        })
                        cursor = 0
                        for delta in _iter_text_chunks(slide):
                            stream_writer({
                                "type": "stream_slide_delta",
                                "delta": delta,
                                "cursor": cursor,
                                **meta,
                            })
                            cursor += len(delta)
                        stream_writer({
                            "type": "stream_slide",
                            "content": slide,
                            **meta,
                        })
                        stream_writer({
                            "type": "stream_slide_done",
                            "content": slide,
                            **meta,
                        })
                done = sum(1 for r in _results if r.get("content"))
                stream_writer({
                    "type": "stream_progress",
                    "file_type": "ppt",
                    "message": f"已生成 {done}/{total} 章节",
                    "current": done,
                    "total": total,
                })
            except Exception:
                logger.exception("[PPT-Parallel] 章节 %d 推送异常", _idx)

    def _push_section_complete(_idx: int, _content: str, _section_title: str = ""):
        """Emit a final PPT section for downstream consumers such as video TTS."""
        if not stream_writer:
            return
        try:
            stream_writer({
                "type": "ppt_section_complete",
                "file_type": "ppt",
                "resource_type": "ppt",
                "section_idx": _idx,
                "section_title": _section_title,
                "section_total": total,
                "content": _content,
            })
        except Exception:
            logger.debug("[PPT-Parallel] section_complete emit failed idx=%s", _idx, exc_info=True)

    async def _review_ppt_section(content: str, section_title: str, format_checked: bool = False) -> dict:
        """调用 reviewer 审核单个章节，返回 {passed, score, feedback}

        Args:
            format_checked: 若 True，表示格式层（layout/分隔符/公式配对/禁用词/字数）
                已由 _quick_check_ppt 通过，reviewer 只需审核语义质量（prompt 更短）。
        """
        try:
            # 根据是否已做格式预检选择不同模板
            template_key = "agent/reviewer_ppt_semantic" if format_checked else "agent/reviewer_ppt"
            reviewer_prompt = load_prompt(template_key)
            prompt_text = fill_prompt(
                reviewer_prompt,
                content=content[:3000],
                topic=topic,
            )
            async with review_sem:
                response = await llm.ainvoke(prompt_text, priority=llm_priority, user_id=user_id, pool="reviewer")
            return _parse_review_response(response.content)
        except Exception as e:
            logger.exception("[PPT-Review] section=%s 审核异常", section_title)
            return {"passed": True, "score": 0, "feedback": f"审核异常: {e}"}

    async def _gen_section(idx: int, section_title: str) -> None:
        """单章节：先推首轮草稿，再给足 3 次 reviewer 语义审核机会。"""
        section_agent_id = f"executor:ppt:section-{idx}"
        _push_agent_event(
            stream_writer,
            section_agent_id,
            f"PPT第 {idx + 1} 章",
            "executor",
            "running",
            f"正在生成「{section_title}」",
            resource_type="ppt",
            current=idx + 1,
            total=total,
        )
        if stream_writer:
            try:
                stream_writer({
                    "type": "stream_progress", "file_type": "ppt",
                    "message": f"正在生成第 {idx + 1}/{total} 章「{section_title}」...",
                    "current": idx + 1, "total": total,
                })
            except Exception:
                logger.debug("[PPT-Section] stream progress push failed idx=%d section=%s", idx, section_title, exc_info=True)

        is_portrait_section = has_portrait and idx == 0

        # 根据章节位置动态裁剪上下文，减少重复 token 注入
        _eff_portrait, _eff_lo, _eff_guidance = _trim_section_context(
            portrait, learning_objectives, guidance,
            section_title, idx, total,
        )
        section_kb = kb
        if not is_portrait_section:
            try:
                section_kb_result = await kb_search(f"{topic} {section_title}", top_k=3, user_id=user_id)
                if section_kb_result and "暂无" not in str(section_kb_result) and "No knowledge" not in str(section_kb_result):
                    section_kb = (
                        f"【本章节知识库检索】\n{section_kb_result}\n\n"
                        f"【课程级知识库检索】\n{kb}"
                    )
            except Exception:
                logger.exception("[PPT-RAG] section knowledge search failed idx=%d section=%s", idx, section_title)

        base_prompt = build_resource_prompt(
            ppt_prompt_key, topic, portrait=_eff_portrait, kb=section_kb, guidance=_eff_guidance,
            feedback=feedback, user_notes=user_notes, custom_prompts=custom_prompts,
            section=section_title, learning_objectives=_eff_lo,
            formula_sheet=formula_sheet,
            ppt_theme_id=ppt_theme_id,
            rag_mode=rag_mode,
        )

        if is_portrait_section:
            parts: list[str] = []
            if guidance:
                parts.append(f"\n\n## 学习指导\n{guidance}")
            if kb:
                parts.append(f"\n\n## 知识库参考资料\n{kb}")
            if feedback:
                parts.append(f"\n\n## 额外反馈\n{feedback}")
            prompt_with_context = (
                f"你是一个贴心的学习导师。为课程「{topic}」撰写 2 页学习引入幻灯片。"
                f"\n\n## 输出格式"
                f"\n直接输出 PPT Markdown，第一个字符必须是 #。用 --- 分隔两页。每页 4-6 条要点，每条 50-90 字，整页可见正文不低于 320 字。"
                f"\n每页最后必须追加一行 `> 讲稿：...`，讲稿 120-220 个中文字符，用于补足课堂讲解和视频朗读。"
                f"\n\n## 第1页：为什么这门课对你很重要"
                f"\n- 用画像中的具体信息（专业、年级等）解释这门课和你的关联"
                f"\n- 让你感受到'这课是为我准备的'"
                f"\n- 语气亲切自然，像导师在课前和学生面对面聊天"
                f"\n\n## 第2页：这门课你将学到什么"
                f"\n- 简要预告本课程涵盖的核心内容（参考课程全景，不要展开讲）"
                f"\n- 说明学完后你能收获什么"
                f"\n- 保持鼓励和温暖的语气"
                f"\n\n## 用户画像\n{portrait}"
                f"\n\n## 课程全景（供预告参考，不要展开讲）\n{course_overview}"
                f"{''.join(parts)}"
            )
        else:
            prompt_with_context = base_prompt

        section_plan = section_guidance_map.get(section_title, "")
        if section_plan:
            prompt_with_context += (
                f"\n\n## 本章节来自课程地图的下发规划（必须遵守）\n"
                f"{section_plan}\n"
                f"- 本章只讲规划中的核心任务，不要抢讲后续章节，也不要退回泛泛概念介绍。"
            )

        if ppt_theme_id:
            prompt_with_context += (
                f"\n\n## 用户选择的 PPT 模板\n"
                f"用户选择的 PPT 模板为：{ppt_theme_id}。\n"
                f"请生成适配该模板风格的 layout/theme/visual 元数据，并在每页标题后一行加入 `<!-- theme: {ppt_theme_id} -->`。"
            )

        # ── 生成 + 审核循环：第一轮先推草稿，等所有章节首轮完成后再进入 reviewer ──
        MAX_REVIEW_CHECKS = 3
        MAX_GENERATION_ROUNDS = MAX_REVIEW_CHECKS + 2
        content = ""
        review_feedback = ""
        final_passed = False
        draft_pushed = False
        review_checks = 0

        def _push_section_replace(_idx: int, _content: str, _title: str):
            """推送章节替换事件 + 新的幻灯片内容"""
            if stream_writer:
                try:
                    stream_writer({
                        "type": "stream_section_replace",
                        "file_type": "ppt",
                        "section_idx": _idx,
                        "section_title": _title,
                    })
                except Exception:
                    logger.debug("[PPT-Section] stream replace push failed idx=%d section=%s", _idx, _title, exc_info=True)
            _push_section(_idx, _content, _title)

        round_idx = 0
        while round_idx < MAX_GENERATION_ROUNDS and review_checks < MAX_REVIEW_CHECKS:
            is_first_round = (round_idx == 0)

            if stream_writer and not is_first_round:
                try:
                    _push_agent_event(
                        stream_writer,
                        section_agent_id,
                        f"PPT第 {idx + 1} 章",
                        "executor",
                        "retrying",
                        f"「{section_title}」审核未通过，正在重写",
                        resource_type="ppt",
                        current=idx + 1,
                        total=total,
                    )
                    stream_writer({
                        "type": "stream_progress", "file_type": "ppt",
                        "message": f"「{section_title}」审核未通过，正在修改...（第{round_idx + 1}轮）",
                        "current": idx + 1, "total": total,
                    })
                except Exception:
                    logger.debug("[PPT-Section] retry progress push failed idx=%d section=%s", idx, section_title, exc_info=True)

            # 拼接审核反馈到 prompt
            if not is_first_round and review_feedback:
                prompt_with_context = (
                    f"{prompt_with_context}\n\n"
                    f"## 上一轮审核未通过，请根据以下意见修改\n"
                    f"{review_feedback}\n\n"
                    f"请重新生成完整内容。"
                )

            # Step A: 生成（连接错误重试一次）
            t0 = time.perf_counter()
            gen_ok = False
            for attempt in range(2):
                try:
                    async with gen_sem:
                        async with _PPT_GLOBAL_GEN_SEM:
                            response = await llm.ainvoke(prompt_with_context, priority=llm_priority, user_id=user_id, pool="ppt")
                    content = _normalize_ppt_content(response.content, section_title)
                    gen_ok = True
                    break
                except Exception as e:
                    elapsed = time.perf_counter() - t0
                    is_conn_error = "RemoteProtocolError" in type(e).__name__ or "ConnectError" in type(e).__name__ or "Timeout" in type(e).__name__ or "timeout" in str(e).lower() or "incomplete" in str(e).lower()
                    if attempt == 0 and is_conn_error:
                        logger.warning("[PPT-Gen] idx=%d section=%s 连接异常，重试中... err=%s", idx, section_title, str(e)[:120])
                        await asyncio.sleep(1.5)
                        continue
                    logger.exception("[PPT-Gen] idx=%d section=%s 生成失败 耗时=%.2fs", idx, section_title, elapsed)
                    fallback_content = _fallback_ppt_section(section_title)
                    _results[idx] = {"idx": idx, "content": fallback_content}
                    if is_first_round:
                        _mark_first_pass_done(idx)
                    _push_agent_event(
                        stream_writer,
                        section_agent_id,
                        f"PPT第 {idx + 1} 章",
                        "executor",
                        "done",
                        f"「{section_title}」使用兜底内容",
                        resource_type="ppt",
                        current=idx + 1,
                        total=total,
                    )
                    _push_section(idx, fallback_content, section_title)
                    _push_section_complete(idx, fallback_content, section_title)
                    return

            if not gen_ok:
                fallback_content = _fallback_ppt_section(section_title)
                _results[idx] = {"idx": idx, "content": fallback_content}
                if is_first_round:
                    _mark_first_pass_done(idx)
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "executor",
                    "done",
                    f"「{section_title}」使用兜底内容",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                )
                _push_section(idx, fallback_content, section_title)
                _push_section_complete(idx, fallback_content, section_title)
                return

            elapsed = time.perf_counter() - t0

            if skip_review_sections:
                _results[idx] = {"idx": idx, "content": content}
                if is_first_round:
                    _mark_first_pass_done(idx)
                _push_section(idx, content, section_title)
                _push_section_complete(idx, content, section_title)
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "executor",
                    "done",
                    f"「{section_title}」已生成",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                    elapsed_ms=int(elapsed * 1000),
                )
                return

            # Step B: 快检
            quick_ok = False
            quick_reason = ""
            if not is_portrait_section:
                quick_ok, quick_reason = _quick_check_ppt(content)
                logger.info("[PPT-Gen] idx=%d section=%s round=%d %s reason=%s 耗时=%.2fs",
                            idx, section_title, round_idx + 1, "快检通过" if quick_ok else "快检未通过", quick_reason or "-", elapsed)
            format_safe = quick_ok or quick_reason in soft_quick_reasons

            if is_first_round and format_safe and not draft_pushed:
                draft_pushed = True
                _results[idx] = {"idx": idx, "content": content, "draft": True}
                _push_section(idx, content, section_title)
            elif is_first_round and not format_safe and not is_portrait_section and not draft_pushed:
                fallback_draft = _fallback_ppt_section(section_title)
                draft_pushed = True
                _results[idx] = {"idx": idx, "content": fallback_draft, "draft": True}
                _push_section(idx, fallback_draft, section_title)

            # 画像引入页不走严格章节 reviewer，但仍只在生成完成后推送。
            if is_portrait_section:
                _results[idx] = {"idx": idx, "content": content}
                if is_first_round:
                    _mark_first_pass_done(idx)
                _push_section(idx, content, section_title)
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "executor",
                    "done",
                    f"「{section_title}」已生成",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                )
                _push_section_complete(idx, content, section_title)
                return

            if is_first_round:
                _mark_first_pass_done(idx)
                await first_pass_event.wait()

            if not format_safe:
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "reviewer",
                    "reviewing",
                    f"「{section_title}」格式快检未通过，准备重写",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                )
                review_result = {
                    "passed": False,
                    "score": 0,
                    "feedback": f"系统格式快检未通过（{quick_reason or 'format_error'}）：请输出完整 PPT Markdown，修复结构、公式闭合、空公式块、HTML/KaTeX 标签泄漏、省略占位、空要点或缺少 `> 讲稿：...` 的问题。每条要点冒号后必须有实质内容。",
                }
            else:
                review_checks += 1
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "reviewer",
                    "reviewing",
                    f"正在审核「{section_title}」（第 {review_checks}/{MAX_REVIEW_CHECKS} 次）",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                )
                review_result = await _review_ppt_section(content, section_title, format_checked=True)
            if review_result.get("passed"):
                final_passed = True
                _results[idx] = {"idx": idx, "content": content}
                if draft_pushed:
                    _push_section_replace(idx, content, section_title)
                else:
                    _push_section(idx, content, section_title)
                _push_section_complete(idx, content, section_title)
                _push_agent_event(
                    stream_writer,
                    section_agent_id,
                    f"PPT第 {idx + 1} 章",
                    "reviewer",
                    "done",
                    f"「{section_title}」审核通过",
                    resource_type="ppt",
                    current=idx + 1,
                    total=total,
                    score=review_result.get("score"),
                )
                logger.info("[PPT-Review] idx=%d section=%s round=%d 审核通过 score=%s",
                            idx, section_title, round_idx + 1, review_result.get("score"))
                break

            review_feedback = review_result.get("feedback", "")
            # 截断反馈防止多轮累积导致 prompt 膨胀
            if len(review_feedback) > 400:
                review_feedback = review_feedback[:400] + "…"
            logger.warning("[PPT-Review] idx=%d section=%s round=%d 审核未通过: %s",
                           idx, section_title, round_idx + 1, review_feedback[:120])
            round_idx += 1

        if not final_passed and not is_portrait_section:
            fallback_content = _fallback_ppt_section(section_title)
            _results[idx] = {"idx": idx, "content": fallback_content}
            if draft_pushed:
                _push_section_replace(idx, fallback_content, section_title)
            else:
                _push_section(idx, fallback_content, section_title)
            _push_section_complete(idx, fallback_content, section_title)
            logger.warning("[PPT-Review] idx=%d section=%s 达最大审核次数 %d/生成轮次 %d，使用安全兜底版本",
                           idx, section_title, review_checks, round_idx)
            _push_agent_event(
                stream_writer,
                section_agent_id,
                f"PPT第 {idx + 1} 章",
                "reviewer",
                "done",
                f"「{section_title}」使用兜底内容",
                resource_type="ppt",
                current=idx + 1,
                total=total,
            )

    # ── 所有章节同时启动（asyncio.gather 天然并行）──
    await asyncio.gather(*[_gen_section(i, s) for i, s in enumerate(sections)])
    _push_agent_event(stream_writer, "executor:ppt", "PPT生成智能体", "executor", "done", "PPT 内容生成完成", resource_type="ppt", current=total, total=total, elapsed_ms=int((time.perf_counter() - _t_total) * 1000))

    # ═══════════════════════════════════
    #  组装最终结果
    # ═══════════════════════════════════
    parts: list[str] = []
    for r in _results:
        for slide in (r.get("content", "") or "").split("\n---\n"):
            slide = slide.strip()
            if not slide:
                continue
            if slide.startswith("<!-- layout:"):
                slide = slide.replace("\n# ", "\n## ", 1)
            elif slide.startswith("# ") and not slide.startswith("## "):
                slide = slide.replace("# ", "## ", 1)
            parts.append(slide)

    combined = "\n---\n".join(parts)
    logger.info("[PPT-Parallel] 章节生成完成 章节数=%d 总页数≈%d 耗时=%.1fs", len(sections), len(parts), time.perf_counter() - _t_total)

    logger.info("[PPT-Parallel] 完成 全程耗时=%.1fs", time.perf_counter() - _t_total)
    return combined


# ═══════════════════════════════════════
#  文档并行生成（与 PPT 对称）
# ═══════════════════════════════════════

async def generate_doc_outline(topic: str, kb: str = "", guidance: str = "", count: int = DOC_DEFAULT_SECTIONS, llm_priority: str = "high", user_id: int = 0) -> list[str]:
    """快速生成文档章节大纲，默认 {count} 章节"""
    prompt = f"""你是一个课程规划师。为以下主题设计文档章节大纲。

主题：{topic}
学习指导：{guidance}

要求：
- 固定 {count} 个章节（必须恰好 {count} 个）
- 章节标题简洁（15 字以内），由浅入深，逻辑连贯
- 覆盖：概念引入 → 原理解析 → 方法技巧 → 案例应用 → 常见误区 → 进阶拓展 → 总结回顾
- 只返回 JSON 字符串数组，不要任何其他文字

返回示例：["线性方程组的定义与几何意义", "高斯消元法的原理与步骤", "矩阵秩与解的判定", "向量空间与线性无关性", "特征值与对角化", "线性变换的几何直观", "工程中的线性代数应用", "易错点辨析与总结"]"""

    try:
        t0 = time.perf_counter()
        response = await llm.ainvoke(prompt, priority=llm_priority, user_id=user_id, pool="document")
        sections = parse_llm_json(response.content)
        if isinstance(sections, list) and len(sections) >= 1:
            defaults = ["核心概念入门", "基本原理推导", "关键方法解析", "典型案例分析", "进阶知识拓展", "实际应用场景", "常见误区辨析", "总结与回顾"]
            while len(sections) < count:
                sections.append(defaults[len(sections) % len(defaults)])
            sections = sections[:count]
            logger.info("[Doc-Outline] 大纲生成 章节数=%d 耗时=%.2fs", len(sections), time.perf_counter() - t0)
            return sections
    except Exception:
        logger.exception("[Doc-Outline] 大纲生成失败，使用默认章节")
    return ["核心概念入门", "基本原理推导", "关键方法解析", "典型案例分析", "进阶知识拓展", "实际应用场景", "常见误区辨析", "总结与回顾"][:count]


async def generate_document_parallel(
    topic: str,
    portrait: str = "",
    kb: str = "",
    guidance: str = "",
    feedback: str = "",
    user_notes: str = "",
    custom_prompts: dict | None = None,
    sections: list[str] | None = None,
    stream_writer=None,
    section_count: int = DOC_DEFAULT_SECTIONS,
    llm_priority: str = "high",
    user_id: int = 0,
    rag_mode: str = "reference",
) -> str:
    """按章节并行生成文档：大纲 → N 条线并行（每条约 400 字），最后按序拼接"""
    _t_total = time.perf_counter()
    _push_agent_event(stream_writer, "executor:document", "文档生成智能体", "executor", "running", "正在启动文档生成", resource_type="document")
    if stream_writer:
        try:
            stream_writer({"type": "stream_start", "file_type": "document"})
        except Exception:
            logger.exception("[Doc-Parallel] stream_start 推送异常")

    if sections is None:
        if stream_writer:
            try:
                _push_agent_event(stream_writer, "executor:document", "文档生成智能体", "executor", "running", "正在规划文档大纲", resource_type="document")
                stream_writer({"type": "stream_progress", "file_type": "document", "message": "正在规划文档大纲..."})
            except Exception:
                logger.exception("[Doc-Parallel] stream_progress 推送异常")
        sections = await generate_doc_outline(topic, kb=kb, guidance=guidance, count=section_count, llm_priority=llm_priority, user_id=user_id)

    outline_lines = [f"第{i+1}章「{s}」" for i, s in enumerate(sections)]
    course_overview = "\n".join(outline_lines)
    course_formula_sheet = build_formula_sheet(topic)

    total = len(sections)
    completed_count = [0]

    async def gen_section(idx: int, section_title: str) -> tuple[int, str]:
        section_agent_id = f"executor:document:section-{idx}"
        _push_agent_event(
            stream_writer,
            section_agent_id,
            f"文档第 {idx + 1} 节",
            "executor",
            "running",
            f"正在撰写「{section_title}」",
            resource_type="document",
            current=idx + 1,
            total=total,
        )
        if stream_writer:
            try:
                stream_writer({
                    "type": "stream_progress",
                    "file_type": "document",
                    "message": f"正在生成第 {idx + 1}/{total} 节「{section_title}」...",
                    "current": idx + 1,
                    "total": total,
                })
            except Exception:
                logger.debug("[Doc-Section] stream progress push failed idx=%d section=%s", idx, section_title, exc_info=True)

        prev_section = sections[idx - 1] if idx > 0 else "（无）"
        next_section = sections[idx + 1] if idx < len(sections) - 1 else "（无）"
        section_formula_sheet = build_formula_sheet(f"{topic} {section_title}") or course_formula_sheet
        formula_guidance = section_formula_sheet or "暂无稳定公式模板；如需公式，请使用标准 LaTeX 并保证公式块闭合。"
        section_kb = kb
        try:
            section_kb_result = await kb_search(f"{topic} {section_title}", top_k=3, user_id=user_id)
            if section_kb_result and "暂无" not in str(section_kb_result) and "No knowledge" not in str(section_kb_result):
                section_kb = (
                    f"【本章节知识库检索】\n{section_kb_result}\n\n"
                    f"【课程级知识库检索】\n{kb}"
                )
        except Exception:
            logger.exception("[Doc-RAG] section knowledge search failed idx=%d section=%s", idx, section_title)
        kb_limit = _int_env("RAG_PROMPT_CONTEXT_CHARS", 2500)
        section_kb_context = _format_kb_context(section_kb[:kb_limit], rag_mode)

        section_prompt = f"""你是一个专业的学习文档撰写者。请为「{topic}」撰写一个章节。

## 当前章节
{section_title}

## 课程全景
{course_overview}

## 上下文
- 前一章：{prev_section}（自然承接）
- 后一章：{next_section}（做好铺垫）
- 严禁重复其他章节内容

## 学习指导
{guidance or '无特殊要求'}

## 知识库参考策略与资料
{section_kb_context}

## 公式速查表（稳定模板，优先引用）
{formula_guidance}

## 公式使用规则
- 如果上方公式速查表非空，优先逐字复用其中的 LaTeX 公式块，不要自行重写同类公式。
- 公式必须使用 `$...$` 或 `$$...$$`，`\\begin{{...}}...\\end{{...}}` 必须完整放在 `$$...$$` 内。
- 中文解释必须写在公式块外，每个核心公式后至少补 1-2 句解释变量含义、适用条件和常见错误。
- 涉及矩阵、行列式、概率、积分、变换等公式密集主题时，优先使用 2x2、3x3 或单变量小例子展开。
- 禁止用 `...`、`依此类推`、`类似可得` 省略推导；必要步骤要完整写出。

直接输出该章节的 Markdown 内容，以 ### {section_title} 开头。"""

        t0 = time.perf_counter()
        try:
            response = await llm.ainvoke(section_prompt, priority=llm_priority, user_id=user_id, pool="document")
            content = response.content.strip()
            _push_text_stream(
                stream_writer,
                "document",
                content,
                section_idx=idx,
                section_title=section_title,
                total=total,
            )
            elapsed = time.perf_counter() - t0
            completed_count[0] += 1
            _push_agent_event(
                stream_writer,
                section_agent_id,
                f"文档第 {idx + 1} 节",
                "executor",
                "done",
                f"「{section_title}」已完成",
                resource_type="document",
                current=completed_count[0],
                total=total,
                elapsed_ms=int(elapsed * 1000),
            )
            logger.info("[Doc-Section] %d/%d idx=%d section=%s len=%d 耗时=%.2fs",
                        completed_count[0], total, idx, section_title, len(content), elapsed)
            return idx, content
        except Exception:
            elapsed = time.perf_counter() - t0
            _push_agent_event(
                stream_writer,
                section_agent_id,
                f"文档第 {idx + 1} 节",
                "executor",
                "failed",
                f"「{section_title}」生成失败",
                resource_type="document",
                current=idx + 1,
                total=total,
                elapsed_ms=int(elapsed * 1000),
            )
            logger.exception("[Doc-Section] idx=%d section=%s 生成失败 耗时=%.2fs", idx, section_title, elapsed)
            return idx, f"### {section_title}\n- 生成失败"

    tasks = [gen_section(i, s) for i, s in enumerate(sections)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda x: x[0])

    parts = [content for _, content in results if content]
    combined = "\n\n".join(parts)
    logger.info("[Doc-Parallel] 完成 章节数=%d 全程耗时=%.1fs", len(sections), time.perf_counter() - _t_total)
    _push_agent_event(stream_writer, "executor:document", "文档生成智能体", "executor", "done", "文档内容生成完成", resource_type="document", current=total, total=total, elapsed_ms=int((time.perf_counter() - _t_total) * 1000))

    if stream_writer:
        try:
            stream_writer({"type": "stream_progress", "file_type": "document", "message": "文档生成完成", "current": total, "total": total})
        except Exception:
            logger.debug("[Doc-Parallel] done progress push failed", exc_info=True)

    return combined


async def executor_node(state: ResourceState) -> dict:
    """并行生成所有资源类型，PPT 通过 get_stream_writer 逐页流式推送"""
    topic = state["topic"]
    resource_types = state.get("resource_types", ["document"])
    portrait = state.get("portrait_context", "")
    kb = state.get("kb_context", "")
    guidance = state.get("learning_guidance", "")
    custom_prompts = state.get("custom_prompts", {}) or {}
    feedback = state.get("review_feedback", "")
    focus_guidance = _build_focus_guidance(state.get("answers", {}) or {})
    user_notes = state.get("user_notes", "")
    rag_mode = _normalize_rag_mode(state.get("rag_mode", "reference"))

    writer = _safe_stream_writer()
    _push_agent_event(
        writer,
        "executor",
        "ExecutorAgent",
        "executor",
        "running",
        f"正在并行调度：{' / '.join(resource_types)}",
        total=len(resource_types),
    )

    # 章节数：根据追问答案的 depth 决定
    answers = state.get("answers", {}) or {}
    depth = answers.get("depth", "standard")
    ppt_section_count = estimate_ppt_section_count(topic, depth)
    doc_section_count = DOC_SECTION_COUNT_BY_DEPTH.get(depth, DOC_DEFAULT_SECTIONS)

    # PPT / 文档 / 图片 → 异步；其余 → 线程池
    has_ppt = "ppt" in resource_types
    has_doc = "document" in resource_types or "case" in resource_types or "reading" in resource_types
    has_image = "image" in resource_types
    thread_types = [rt for rt in resource_types if rt not in ("ppt", "document", "case", "reading", "image")]

    # 非 PPT/文档 类型线程池并行
    prompts = {
        rt: build_resource_prompt(
            rt, topic, portrait=portrait, kb=kb, guidance=guidance,
            feedback=feedback, user_notes=user_notes,
            exam_count=state.get("exam_count", "5"),
            question_types=state.get("exam_question_types", "single_choice, multi_choice, true_false"),
            difficulty=state.get("exam_difficulty", "medium"),
            custom_prompts=custom_prompts, focus_guidance=focus_guidance,
            ppt_theme_id=state.get("ppt_theme_id", ""),
            rag_mode=rag_mode,
        )
        for rt in thread_types
    }

    user_id = state.get("user_id", "0")
    user_id_int = int(user_id)
    max_workers = min(len(thread_types), 20)
    llm_priority = state.get("llm_priority", "high")

    def gen_one_sync(rt: str) -> tuple[str, str]:
        t_start = time.perf_counter()
        _push_agent_event(writer, f"executor:{rt}", f"{rt}生成智能体", "executor", "running", f"正在生成 {rt}", resource_type=rt)
        try:
            response = llm.invoke(prompts[rt], priority=llm_priority, user_id=user_id_int, pool="thread")
            result = rt, response.content
            elapsed = time.perf_counter() - t_start
            _push_agent_event(writer, f"executor:{rt}", f"{rt}生成智能体", "executor", "done", f"{rt} 生成完成", resource_type=rt, elapsed_ms=int(elapsed * 1000))
            logger.info(f"[Executor] {rt} 生成完成 耗时={elapsed:.2f}s")
            return result
        except Exception as e:
            elapsed = time.perf_counter() - t_start
            _push_agent_event(writer, f"executor:{rt}", f"{rt}生成智能体", "executor", "failed", f"{rt} 生成失败", resource_type=rt, elapsed_ms=int(elapsed * 1000))
            logger.exception(f"[Executor] {rt} 调用失败 耗时={elapsed:.2f}s")
            return rt, f"[生成失败: {e}]"

    t_gen_start = time.perf_counter()

    # PPT / 文档 / 其余 全部并行执行
    loop = asyncio.get_running_loop()

    def _emit_resource_complete(rt: str, content, file_url: str | None = None):
        if not writer or content is None:
            return
        try:
            writer({
                "type": "resource_complete",
                "resource_type": rt,
                "file_type": rt,
                "content": content,
                "file_url": file_url or "",
            })
        except Exception:
            logger.debug("[Executor] resource_complete emit failed rt=%s", rt, exc_info=True)

    async def _run_ppt():
        if not has_ppt:
            return None
        content = await generate_ppt_parallel(
            topic, portrait=portrait, kb=kb, guidance=guidance,
            feedback=feedback, user_notes=user_notes,
            custom_prompts=custom_prompts,
            stream_writer=writer,
            section_count=ppt_section_count,
            ppt_prompt_key=state.get("ppt_prompt_key", "ppt"),
            llm_priority=llm_priority,
            user_id=user_id_int,
            skip_review_sections=bool(state.get("skip_review", False)),
            ppt_theme_id=state.get("ppt_theme_id", ""),
            rag_mode=rag_mode,
        )
        _emit_resource_complete("ppt", content)
        return content

    async def _run_doc():
        if not has_doc:
            return None
        content = await generate_document_parallel(
            topic, portrait=portrait, kb=kb, guidance=guidance,
            feedback=feedback, user_notes=user_notes,
            custom_prompts=custom_prompts,
            stream_writer=writer,
            section_count=doc_section_count,
            llm_priority=llm_priority,
            user_id=user_id_int,
            rag_mode=rag_mode,
        )
        _emit_resource_complete("document", content)
        return content

    async def _run_image():
        if not has_image:
            return None
        _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "running", "正在生成图片提示词", resource_type="image")
        img_prompt_text = build_resource_prompt(
            "image", topic, portrait=portrait, kb=kb, guidance=guidance,
            feedback=feedback, user_notes=user_notes,
            custom_prompts=custom_prompts, focus_guidance=focus_guidance,
            ppt_theme_id=state.get("ppt_theme_id", ""),
            rag_mode=rag_mode,
        )
        try:
            response = await llm.ainvoke(img_prompt_text, priority=llm_priority, user_id=user_id_int, pool="thread")
            image_prompt = response.content.strip()[:900]
        except Exception:
            logger.exception("[Executor] 图片 prompt 生成失败")
            _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "failed", "图片提示词生成失败", resource_type="image")
            return "image:error", {"prompt": "", "url": ""}

        try:
            _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "running", "正在调用图片生成服务", resource_type="image")
            from backend.src.service.image import service as image_service
            images = await image_service.generate(image_prompt, str(user_id_int), aspect_ratio="16:9", img_count=2, save_history=False, chat_group_id=int(state.get("chat_group_id", 0)))
            if images and len(images) > 0:
                _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "done", "图片生成完成", resource_type="image")
                return "image:image", {"prompt": image_prompt, "url": images[0].get("url", "")}
            _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "done", "图片提示词已生成", resource_type="image")
            return "image:image", {"prompt": image_prompt, "url": ""}
        except Exception as e:
            logger.exception(f"[Executor] 图片生成失败: {e}")
            _push_agent_event(writer, "executor:image", "图片生成智能体", "executor", "failed", "图片生成失败", resource_type="image")
            return "image:image", {"prompt": image_prompt, "url": ""}

    ppt_coro = _run_ppt()
    doc_coro = _run_doc()
    image_coro = _run_image()

    if thread_types:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            other_futures = asyncio.gather(
                *[loop.run_in_executor(pool, gen_one_sync, rt) for rt in thread_types]
            )
            ppt_content, doc_content, image_result, other_results = await asyncio.gather(ppt_coro, doc_coro, image_coro, other_futures)
    else:
        other_results = []
        ppt_content, doc_content, image_result = await asyncio.gather(ppt_coro, doc_coro, image_coro)

    logger.info("[Executor] 全部生成完成 并行总耗时=%.2fs types=%s", time.perf_counter() - t_gen_start, resource_types)
    _push_agent_event(
        writer,
        "executor",
        "ExecutorAgent",
        "executor",
        "done",
        "并行生成阶段完成",
        elapsed_ms=int((time.perf_counter() - t_gen_start) * 1000),
    )

    retry = state.get("retry_count", 0)
    if feedback:
        retry += 1

    generated = {}
    file_urls = {}
    # 异步图片结果
    if image_result:
        rt, content = image_result
        actual_rt = rt.replace("image:", "")
        generated[actual_rt] = content.get("prompt", "")
        if content.get("url"):
            file_urls[actual_rt] = content["url"]
    for rt, content in other_results:
        if rt.startswith("image:"):
            actual_rt = rt.replace("image:", "")
            generated[actual_rt] = content.get("prompt", "")
            if content.get("url"):
                file_urls[actual_rt] = content["url"]
        else:
            if rt == "mindmap":
                _push_text_stream(writer, "mindmap", content)
            generated[rt] = content
    if ppt_content:
        generated["ppt"] = ppt_content
    if doc_content:
        generated["document"] = doc_content

    return {
        "generated_resources": generated,
        "file_urls": file_urls,
        "retry_count": retry,
        "review_feedback": "",
    }


def _generate_image_sync(prompt_text: str, user_id: str, user_id_int: int = 0, llm_priority: str = "high") -> tuple[str, dict]:
    """两阶段图片生成：LLM 产出 prompt → image service 生图（线程内）"""
    try:
        response = llm.invoke(prompt_text, priority=llm_priority, user_id=user_id_int, pool="thread")
        image_prompt = response.content.strip()
    except Exception as e:
        logger.exception("图片 prompt 生成失败")
        return ("image:error", {"prompt": "", "url": ""})

    try:
        from backend.src.service.image import service as image_service
        image_prompt = image_prompt[:900]
        loop = asyncio.new_event_loop()
        try:
            images = loop.run_until_complete(
                image_service.generate(image_prompt, user_id, aspect_ratio="16:9", img_count=2, save_history=False)
            )
        finally:
            loop.close()
        if images and len(images) > 0:
            return ("image:image", {"prompt": image_prompt, "url": images[0].get("url", "")})
    except Exception as e:
        logger.exception(f"图片生成失败: {e}")

    return ("image:image", {"prompt": image_prompt, "url": ""})


def _parse_review_response(raw: str) -> dict:
    """解析 reviewer 返回的 JSON，容错处理。
    支持新格式（逐题）和旧格式（整批），统一返回含 questions 字段的 dict。"""
    try:
        result = parse_llm_json(raw)
        if not isinstance(result, dict):
            return {"passed": True, "score": 70, "feedback": raw, "questions": []}
        # exercise 新格式：包含逐题结果
        if "questions" in result and isinstance(result["questions"], list):
            return {
                "passed": result.get("overall_passed", True),
                "score": result.get("overall_score", 70),
                "feedback": result.get("overall_feedback", ""),
                "questions": result["questions"],
            }
        # 旧格式兼容（其他资源类型）
        return {
            "passed": result.get("passed", True),
            "score": result.get("score", 70),
            "feedback": result.get("feedback", ""),
            "questions": [],
        }
    except json.JSONDecodeError:
        return {"passed": True, "score": 70, "feedback": raw, "questions": []}


# 资源类型 → 专用审核员
_REVIEWER_MAP = {
    "document": "agent/reviewer_document",
    "case": "agent/reviewer_document",
    "reading": "agent/reviewer_document",
    "ppt": "agent/reviewer_ppt",
    "exercise": "agent/reviewer_exam",
    "mindmap": "agent/mindmap_reviewer",
    "image": "agent/reviewer_image",
}


async def reviewer_node(state: ResourceState) -> dict:
    """ReviewerAgent: 每种资源类型由专用审核员独立审查，并行执行"""
    writer = _safe_stream_writer()
    generated = state.get("generated_resources", {})
    llm_priority = state.get("llm_priority", "high")
    user_id_int = int(state.get("user_id", 0))
    _push_agent_event(writer, "reviewer", "ReviewerAgent", "reviewer", "reviewing", "正在进行质量审核", total=len(generated))

    async def review_one(rt: str, content: str) -> dict:
        _push_agent_event(writer, f"reviewer:{rt}", f"{rt}审核智能体", "reviewer", "reviewing", f"正在审核 {rt}", resource_type=rt)
        # PPT / 文档 已在 generate_*_parallel 内部逐章节审核（生成→审核→重生成循环），跳过全局审核
        if rt in ("ppt", "document", "case", "reading"):
            _push_agent_event(writer, f"reviewer:{rt}", f"{rt}审核智能体", "reviewer", "done", f"{rt} 已通过内置审核", resource_type=rt, score=100)
            return {"passed": True, "score": 100, "feedback": ""}
        # API 生成的图片跳过文本审核
        file_urls = state.get("file_urls", {})
        if file_urls.get(rt):
            _push_agent_event(writer, f"reviewer:{rt}", f"{rt}审核智能体", "reviewer", "done", f"{rt} 自动通过审核", resource_type=rt, score=100)
            return {"passed": True, "score": 100, "feedback": "API 生成，自动通过"}
        reviewer_path = _REVIEWER_MAP.get(rt, "agent/reviewer_document")
        if not content:
            _push_agent_event(writer, f"reviewer:{rt}", f"{rt}审核智能体", "reviewer", "done", f"{rt} 无需审核", resource_type=rt, score=100)
            return {"passed": True, "score": 100, "feedback": ""}
        try:
            content_snippet = content[:3000]
            prompt_text = fill_prompt(
                load_prompt(reviewer_path),
                content=content_snippet,
                topic=state.get("topic", ""),
                kb_context=state.get("kb_context", "暂无相关知识库资料"),
            )
            response = await (llm_vision if rt == "image" else llm).ainvoke(prompt_text, priority=llm_priority, user_id=user_id_int, pool="reviewer")
            result = _parse_review_response(response.content)
            _push_agent_event(
                writer,
                f"reviewer:{rt}",
                f"{rt}审核智能体",
                "reviewer",
                "done" if result.get("passed") else "retrying",
                f"{rt} 审核{'通过' if result.get('passed') else '需要修订'}",
                resource_type=rt,
                score=result.get("score"),
            )
            logger.info(f"[审核] {rt}: passed={result.get('passed')} score={result.get('score')}")
            return result
        except Exception as e:
            logger.exception(f"[审核] {rt} 失败")
            _push_agent_event(writer, f"reviewer:{rt}", f"{rt}审核智能体", "reviewer", "failed", f"{rt} 审核异常", resource_type=rt)
            return {"passed": True, "score": 0, "feedback": f"审核异常: {e}"}

    tasks = [review_one(rt, content) for rt, content in generated.items()]
    if not tasks:
        _push_agent_event(writer, "reviewer", "ReviewerAgent", "reviewer", "done", "没有需要审核的资源")
        return {"review_passed": True, "review_feedback": ""}

    results = await asyncio.gather(*tasks)

    # 汇总
    feedback_parts = []
    all_passed = True
    question_results = []
    for (rt, _), result in zip(generated.items(), results):
        passed = result.get("passed", False)
        if isinstance(passed, str):
            passed = passed.lower() in ("true", "yes", "1", "是", "pass")
        if not passed:
            all_passed = False
            feedback_parts.append(f"[{rt}] {result.get('feedback', '')}")
        # 收集 per-question 审核结果（exercise 类型）
        for q in result.get("questions", []):
            question_results.append(q)

    _push_agent_event(
        writer,
        "reviewer",
        "ReviewerAgent",
        "reviewer",
        "done" if all_passed else "retrying",
        "质量审核通过" if all_passed else "审核发现问题，准备重新生成",
    )

    return {
        "review_passed": all_passed,
        "review_feedback": "\n".join(feedback_parts),
        "reviewer_questions": question_results,
    }


def _build_focus_guidance(answers: dict) -> str:
    """从追问答案提取聚焦方向和深度，构建 prompt 约束指令。
    无 answers 时返回空字符串，不影响常规生成。
    """
    if not answers:
        return ""

    # 提取所有 focus 值
    focus_vals: list[str] = []
    for key, val in answers.items():
        if key.startswith("focus") or key == "focus":
            if isinstance(val, list):
                focus_vals.extend(str(v) for v in val)
            elif val:
                focus_vals.append(str(val))

    depth = answers.get("depth", "")
    if not focus_vals and not depth:
        return ""

    parts: list[str] = ["\n\n【学习方向与深度约束 — 必须严格遵守】"]
    if focus_vals:
        kw = "、".join(focus_vals)
        parts.append(f"- 聚焦主题：{kw}")
        parts.append("- 仅生成上述方向的内容，不要展开无关话题")
    if depth:
        depth_map = {
            "overview": "- 深度：极速概览，只讲核心概念和关键结论，省略推导和案例",
            "standard": "- 深度：标准讲解，涵盖原理和应用，适量举例",
            "deep": "- 深度：逐页详解，包含完整推导、案例分析和深入讨论",
        }
        parts.append(depth_map.get(depth, f"- 深度：{depth}"))

    parts.append("- 请根据以上约束减少内容体量，精简字数，不要为凑篇幅而添加无关信息")
    return "\n".join(parts)


# ═══════════════════════════════════════
#  Router
# ═══════════════════════════════════════

def should_continue(state: ResourceState) -> str:
    if state.get("review_passed"):
        return "end"
    if state.get("retry_count", 0) >= 2:
        return "end"
    return "executor"


def should_review(state: ResourceState) -> str:
    """跳过审核可省掉一轮 LLM 调用，适用于学习路径等对质量要求不极端的场景"""
    if state.get("skip_review"):
        return "end"
    return "reviewer"


# ═══════════════════════════════════════
#  Graph
# ═══════════════════════════════════════

def build_graph():
    workflow = StateGraph(ResourceState)

    workflow.add_node("leader", leader_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("reviewer", reviewer_node)

    workflow.add_edge(START, "leader")
    workflow.add_edge("leader", "executor")
    workflow.add_conditional_edges(
        "executor",
        should_review,
        {"reviewer": "reviewer", "end": END},
    )
    workflow.add_conditional_edges(
        "reviewer",
        should_continue,
        {"executor": "executor", "end": END},
    )

    return workflow.compile()


resource_graph = build_graph()
