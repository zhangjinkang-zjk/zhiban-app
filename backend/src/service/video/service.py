"""学习视频 HTML 生成服务 — 先出骨架，后台补音频"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import httpx

from backend.src.models.video_model import Video
from backend.src.models.resource_model import GeneratedResource
from backend.src.models.notification_model import Notification
from backend.src.models.chat_history_model import ChatHistory
from backend.src.ai_core.llm_config import llm
from backend.src.utils.prompt_loader import load_prompt, fill_prompt
from backend.src.utils.exceptions import ServiceError
from backend.src.utils.chat_utils import allocate_chat_group_id
from backend.src.utils.redis_client import notify_sse as _redis_notify_sse, subscribe_sse, unsubscribe_sse
from backend.src.utils.constants import AUDIO_DIR, VIDEOS_DIR

logger = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = SRC_DIR / "ai_core" / "prompts" / "presentation" / "template.html"
TEMPLATE_VIDEO_PATH = SRC_DIR / "ai_core" / "prompts" / "presentation" / "template_video.html"
PRESENTATION_TEMPLATE_VERSION = "visual-v6"
VIDEO_TEMPLATE_VERSION = "video-v4"
DEFAULT_VIDEO_VOICE = "zh-CN-XiaoxiaoNeural"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


VIDEO_AUDIO_AUTO_GENERATE = _bool_env("VIDEO_AUDIO_AUTO_GENERATE", True)
AUDIO_FRONTLOAD_CHAPTERS = max(0, _int_env("VIDEO_AUDIO_FRONTLOAD_CHAPTERS", 0))
AUDIO_FRONTLOAD_SLIDES = max(0, _int_env("VIDEO_AUDIO_FRONTLOAD_SLIDES", 0))
AUDIO_CHAPTER_CONCURRENCY = max(1, _int_env("VIDEO_AUDIO_CHAPTER_CONCURRENCY", 4))
AUDIO_SLIDE_CONCURRENCY = max(1, _int_env("VIDEO_AUDIO_SLIDE_CONCURRENCY", 6))
VIDEO_HTML_FLUSH_INTERVAL_MS = max(0, _int_env("VIDEO_HTML_FLUSH_INTERVAL_MS", 900))
VIDEO_INTRO_PREWARM_TTL_SECONDS = max(60, _int_env("VIDEO_INTRO_PREWARM_TTL_SECONDS", 1800))
_VIDEO_AUDIO_FLUSH_LOCKS: dict[int, asyncio.Lock] = {}
_VIDEO_HTML_LAST_FLUSH_AT: dict[int, float] = {}
_HTML_TEMPLATE_VERSION_BY_FILE: dict[str, str] = {}
_PORTRAIT_INTRO_PREWARM: dict[str, dict] = {}
_PORTRAIT_INTRO_PREWARM_TASKS: dict[str, asyncio.Task] = {}


def _tts_cache_key(text: str, voice: str) -> str:
    return hashlib.md5(f"{text}_{voice}".encode()).hexdigest()[:12]


def _portrait_intro_cache_key(user_id: int | str, topic: str, voice: str) -> str:
    raw = f"{user_id}|{voice}|{str(topic or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _get_cached_portrait_intro(cache_key: str) -> dict | None:
    item = _PORTRAIT_INTRO_PREWARM.get(cache_key)
    if not item:
        return None
    if item.get("expires_at", 0) < time.monotonic():
        _PORTRAIT_INTRO_PREWARM.pop(cache_key, None)
        return None
    intro = item.get("intro")
    return copy.deepcopy(intro) if intro else None


def _store_cached_portrait_intro(cache_key: str, intro: dict) -> None:
    _PORTRAIT_INTRO_PREWARM[cache_key] = {
        "intro": copy.deepcopy(intro),
        "expires_at": time.monotonic() + VIDEO_INTRO_PREWARM_TTL_SECONDS,
    }
    if len(_PORTRAIT_INTRO_PREWARM) > 128:
        oldest_key = min(_PORTRAIT_INTRO_PREWARM, key=lambda k: _PORTRAIT_INTRO_PREWARM[k].get("expires_at", 0))
        _PORTRAIT_INTRO_PREWARM.pop(oldest_key, None)


def _schedule_portrait_intro_prewarm(topic: str, user, user_id: int, voice: str = DEFAULT_VIDEO_VOICE) -> None:
    if not user:
        return
    cache_key = _portrait_intro_cache_key(user_id, topic, voice)
    cached = _get_cached_portrait_intro(cache_key)
    if cached and cached.get("is_audio_ready"):
        return
    existing = _PORTRAIT_INTRO_PREWARM_TASKS.get(cache_key)
    if existing and not existing.done():
        return

    task = asyncio_create_task(_prewarm_portrait_intro(topic, user, user_id, voice, cache_key))
    if not task:
        return
    _PORTRAIT_INTRO_PREWARM_TASKS[cache_key] = task

    def _cleanup(done: asyncio.Task):
        if _PORTRAIT_INTRO_PREWARM_TASKS.get(cache_key) is done:
            _PORTRAIT_INTRO_PREWARM_TASKS.pop(cache_key, None)
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("[视频] 画像引入预热任务已取消 topic=%s", topic)
        except Exception:
            logger.debug("[视频] 画像引入预热失败 topic=%s", topic, exc_info=True)

    task.add_done_callback(_cleanup)


async def _get_prewarmed_portrait_intro(topic: str, user_id: int, voice: str, wait_ms: int = 1200) -> dict | None:
    cache_key = _portrait_intro_cache_key(user_id, topic, voice)
    cached = _get_cached_portrait_intro(cache_key)
    if cached:
        return cached

    task = _PORTRAIT_INTRO_PREWARM_TASKS.get(cache_key)
    if task and not task.done() and wait_ms > 0:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_ms / 1000)
        except asyncio.TimeoutError:
            logger.debug("[视频] 等待画像引入预热超时 topic=%s wait_ms=%s", topic, wait_ms)
        except Exception:
            logger.debug("[视频] 等待画像引入预热失败 topic=%s", topic, exc_info=True)
    return _get_cached_portrait_intro(cache_key)


async def _prewarm_portrait_intro(topic: str, user, user_id: int, voice: str, cache_key: str) -> dict | None:
    t0 = time.perf_counter()
    intro = await _build_portrait_intro(topic, user)
    if not intro:
        return None

    _store_cached_portrait_intro(cache_key, intro)
    raw_text = intro.get("_raw_text", "")
    if VIDEO_AUDIO_AUTO_GENERATE and raw_text:
        segments = await _prewarm_intro_tts_cache(raw_text, voice, user_id)
        if segments:
            _patch_intro_audio(intro, {"sections": segments})
            slides = intro.get("slides", [])
            intro["is_audio_ready"] = bool(slides) and all(s.get("audio_url") for s in slides)
            _store_cached_portrait_intro(cache_key, intro)

    logger.info(
        "[视频] 画像引入预热完成 user=%s topic=%s audio_ready=%s cost=%.1fs",
        user_id, str(topic)[:40], intro.get("is_audio_ready"), time.perf_counter() - t0,
    )
    return copy.deepcopy(intro)


async def _prewarm_intro_tts_cache(raw_text: str, voice: str, user_id: int) -> list[dict]:
    from backend.src.service.narration.service import _generate_tts
    from backend.src.utils.tts_utils import parse_text_sections

    sections = parse_text_sections(raw_text or "")
    if not sections:
        return []

    cache_dir = AUDIO_DIR / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    async def _one(i: int, section: dict) -> dict | None:
        text = section.get("text", "")
        if not text:
            return None
        cache_key = _tts_cache_key(text, voice)
        mp3_path = cache_dir / f"{cache_key}.mp3"
        json_path = cache_dir / f"{cache_key}.json"

        word_timestamps = None
        if mp3_path.exists() and json_path.exists():
            try:
                word_timestamps = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("[视频] 画像引入 TTS 缓存读取失败 json=%s", json_path, exc_info=True)
                word_timestamps = None

        if word_timestamps is None:
            word_timestamps = await _generate_tts(text, voice, str(mp3_path), user_id=user_id)
        if word_timestamps is None:
            return None

        return {
            "index": i,
            "title": section.get("title") or "",
            "text": text,
            "audio_url": f"/static/audio/_cache/{cache_key}.mp3",
            "duration_ms": _real_duration_from_timestamps(word_timestamps, section.get("duration_ms", 5000)),
            "word_timestamps": word_timestamps,
        }

    raw_results = await asyncio.gather(*(_one(i, s) for i, s in enumerate(sections)), return_exceptions=True)
    results: list[dict] = []
    for result in raw_results:
        if isinstance(result, dict):
            results.append(result)
        elif isinstance(result, Exception):
            logger.debug(
                "[视频] 画像引入 TTS 预热分段失败",
                exc_info=(type(result), result, result.__traceback__),
            )
    results.sort(key=lambda item: item.get("index", 0))
    return results


def _get_video_audio_flush_lock(record_id: int) -> asyncio.Lock:
    lock = _VIDEO_AUDIO_FLUSH_LOCKS.get(record_id)
    if lock is None:
        lock = asyncio.Lock()
        _VIDEO_AUDIO_FLUSH_LOCKS[record_id] = lock
    return lock


def _write_text_atomic(file_path: Path, text: str, encoding: str = "utf-8") -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_name(f".{file_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_path.write_text(text, encoding=encoding)
        tmp_path.replace(file_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("清理临时 HTML 文件失败 path=%s", tmp_path)


def _should_flush_video_html(record_id: int, force: bool = False) -> bool:
    now = time.monotonic()
    if force or VIDEO_HTML_FLUSH_INTERVAL_MS <= 0:
        _VIDEO_HTML_LAST_FLUSH_AT[record_id] = now
        return True
    last = _VIDEO_HTML_LAST_FLUSH_AT.get(record_id, 0.0)
    if (now - last) * 1000 >= VIDEO_HTML_FLUSH_INTERVAL_MS:
        _VIDEO_HTML_LAST_FLUSH_AT[record_id] = now
        return True
    return False


@lru_cache(maxsize=8)
def _read_template_cached(path: str, mtime_ns: int) -> str:
    return Path(path).read_text(encoding="utf-8")


def _read_template(template_path: Path) -> str:
    stat = template_path.stat()
    return _read_template_cached(str(template_path), stat.st_mtime_ns)


def _remember_template_version(file_path: Path, version: str) -> None:
    _HTML_TEMPLATE_VERSION_BY_FILE[str(file_path)] = version


def _detect_template_version(file_path: Path) -> str:
    cache_key = str(file_path)
    cached = _HTML_TEMPLATE_VERSION_BY_FILE.get(cache_key)
    if cached:
        return cached
    version = PRESENTATION_TEMPLATE_VERSION
    try:
        head = file_path.read_text(encoding="utf-8", errors="ignore")[:220]
        if f"template-version:{VIDEO_TEMPLATE_VERSION}" in head or "template-version:video-v" in head:
            version = VIDEO_TEMPLATE_VERSION
    except Exception:
        logger.warning("读取 HTML 模板版本失败 path=%s", file_path)
    _HTML_TEMPLATE_VERSION_BY_FILE[cache_key] = version
    return version


def _format_duration(seconds: int | float | str | None) -> str:
    try:
        raw = str(seconds or "").strip()
        if ":" in raw:
            parts = [int(p) for p in raw.split(":") if p != ""]
            total = 0
            for part in parts:
                total = total * 60 + part
        else:
            total = int(float(raw or 0))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_view_count(count: int | float | str | None) -> str:
    try:
        value = int(float(count or 0))
    except (TypeError, ValueError):
        return ""
    if value >= 10000:
        wan = f"{value / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{wan}万次"
    return f"{value}次" if value > 0 else ""


class ExternalVideoService:
    """Lightweight online teaching video search, currently backed by Bilibili web search."""

    SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"

    @staticmethod
    async def search(topic: str, max_results: int = 3) -> list[dict]:
        query = str(topic or "").strip()
        if not query:
            return []
        max_results = max(1, min(int(max_results or 3), 5))
        params = {
            "search_type": "video",
            "keyword": f"{query} 教学",
            "page": 1,
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(ExternalVideoService.SEARCH_URL, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            logger.info("[ExternalVideo] search failed topic=%s", query, exc_info=True)
            return []

        items = ((payload or {}).get("data") or {}).get("result") or []
        videos: list[dict] = []
        for item in items[:max_results]:
            bvid = item.get("bvid") or ""
            page_url = item.get("arcurl") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
            embed_url = f"https://player.bilibili.com/player.html?bvid={bvid}" if bvid else page_url
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            videos.append({
                "title": title,
                "author": item.get("author") or "",
                "duration": item.get("duration") or 0,
                "view_count": item.get("play") or 0,
                "description": item.get("description") or "",
                "cover_url": item.get("pic") or "",
                "page_url": page_url,
                "embed_url": embed_url,
                "source": "bilibili",
                "source_label": "B站",
            })
        return videos

    @staticmethod
    async def search_and_save(topic: str, user_id: int, max_results: int = 3, chat_group_id: int = 0) -> list[dict]:
        videos = await ExternalVideoService.search(topic, max_results=max_results)
        saved: list[dict] = []
        for v in videos:
            record = await GeneratedResource.create(
                topic=topic,
                resource_type="external_video",
                content=json.dumps(v, ensure_ascii=False),
                file_url=v.get("page_url") or v.get("embed_url") or "",
                cover_url=v.get("cover_url") or "",
                review_passed=True,
                retry_count=0,
                user_id=user_id,
            )
            saved.append({
                "resource_id": record.id,
                "topic": record.topic,
                "resource_type": "external_video",
                "file_type": "external_video",
                "filename": f"推荐视频: {(v.get('title') or '')[:40]}",
                "file_url": record.file_url,
                "cover_url": v.get("cover_url", ""),
                "embed_url": v.get("embed_url", ""),
                "title": v.get("title", ""),
                "author": v.get("author", ""),
                "duration": v.get("duration"),
                "duration_text": _format_duration(v.get("duration")),
                "view_count": v.get("view_count", 0),
                "view_count_text": _format_view_count(v.get("view_count")),
                "description": v.get("description", ""),
                "source": v.get("source_label", ""),
                "source_label": v.get("source_label", ""),
                "preview_url": v.get("embed_url", ""),
            })

        if saved and chat_group_id:
            try:
                await ChatHistory.create(
                    user_id=user_id,
                    chat_group_id=chat_group_id,
                    req=f"搜索在线视频：{topic}",
                    res=json.dumps({"type": "external_videos", "resources": saved}, ensure_ascii=False),
                )
            except Exception:
                logger.debug("[ExternalVideo] save chat history failed", exc_info=True)
        return saved


# ═══════════════════════════════════════════════
#  预览 — 展示可用章节让用户选择
# ═══════════════════════════════════════════════

async def preview(topic: str, user_id: int) -> dict:
    """预览话题的可用章节及其内容大纲"""
    doc, mindmap_data, ppt_data = await _fetch_resources(topic, user_id)
    chapters = []

    # intro — 从 document ## 标题提取
    if doc:
        content = doc.content or ""
        raw_parts = re.split(r"\n(?=## )", content.strip())
        if len(raw_parts) <= 1:
            raw_parts = re.split(r"\n(?=# )", content.strip())
        slides = []
        for part in raw_parts:
            part = part.strip()
            if not part:
                continue
            lines = part.split("\n")
            title = lines[0].lstrip("#").strip()
            summary = ""
            for l in lines[1:]:
                s = l.strip().lstrip("-* ").strip()
                if s:
                    summary = s[:80]
                    break
            slides.append({"title": title, "summary": summary})
        chapters.append({
            "id": "intro",
            "title": "学科介绍",
            "slide_count": len(slides),
            "slides": slides,
        })

    # mindmap
    if mindmap_data:
        from backend.src.utils.mindmap import parse_mindmap_text

        def _count_nodes(node) -> int:
            return 1 + sum(_count_nodes(c) for c in node.get("children", []))
        parsed = parse_mindmap_text(mindmap_data.content or "")
        node_count = _count_nodes(parsed) if parsed else 0
        top_topics = [c.get("topic", "") for c in (parsed.get("children", [])[:5])] if parsed else []
        chapters.append({
            "id": "mindmap",
            "title": "思维导图",
            "node_count": node_count,
            "top_topics": top_topics,
        })

    # PPT
    if ppt_data:
        from backend.src.utils.tts_utils import parse_slides
        slides_meta = parse_slides(ppt_data.content or "")
        slides = [{"title": m.get("title", ""), "bullet_count": len(m.get("bullets", []))} for m in slides_meta]
        chapters.append({
            "id": "ppt",
            "title": "PPT讲解",
            "slide_count": len(slides),
            "slides": slides,
        })

    return {"chapters": chapters}




async def generate_questions(topic: str, user_id: int, chat_group_id: int = 0, voice: str = DEFAULT_VIDEO_VOICE) -> dict:
    """分析资源内容或话题本身，生成 2-3 个选择题帮助用户聚焦视频方向"""
    from backend.src.models.usermodel import User

    # 画像上下文
    portrait_context = ""
    user = await User.filter(id=user_id).first()
    if user:
        parts = []
        if user.major:
            parts.append(f"专业：{user.major}")
        if user.grade:
            parts.append(f"年级：{user.grade}")
        portrait_context = "；".join(parts) if parts else ""
        _schedule_portrait_intro_prewarm(topic, user, user_id, voice or DEFAULT_VIDEO_VOICE)

    questions_list = _normalize_video_questions(
        await _generate_questions_from_topic(topic, portrait_context, user_id=user_id),
        topic,
    )

    if not questions_list:
        questions_list = [_default_depth_question()]

    # 写入聊天历史 — 新对话自动分配 chat_group_id
    chat_group_id = chat_group_id if chat_group_id and chat_group_id > 0 else await allocate_chat_group_id(user_id)
    logger.info("generate_questions() user_id=%d chat_group_id=%d topic=%s", user_id, chat_group_id, topic[:60])
    try:
        await ChatHistory.create(
            user=user,
            chat_group_id=chat_group_id,
            req="",
            res=json.dumps({"type": "presentation_questions", "topic": topic, "questions": questions_list, "_video_hint": "学习视频"}, ensure_ascii=False),
        )
    except Exception:
        logger.exception("保存追问到聊天历史失败")

    return {"questions": questions_list, "chat_group_id": chat_group_id}


def _default_depth_question() -> dict:
    return {
        "id": "depth",
        "question": "需要多深的内容？",
        "multi": False,
        "options": [
            {"label": "极速概览，了解核心概念", "value": "overview"},
            {"label": "标准讲解，理解原理和应用", "value": "standard"},
            {"label": "逐页详解，包含推导和案例", "value": "deep"},
        ],
    }


def _default_focus_question(topic: str = "") -> dict:
    base = re.sub(r"\s+", "", str(topic or "").strip())[:12] or "本主题"
    options = [
        f"核心概念 — 围绕{base}讲清定义、术语和整体框架",
        f"原理流程 — 拆解{base}的关键机制、步骤和因果关系",
        f"方法操作 — 梳理{base}常用方法、步骤和适用条件",
        f"例题案例 — 用典型题目或实际场景串联{base}应用",
        f"易错对比 — 对比{base}相似概念并指出常见误区",
    ]
    return {
        "id": "focus",
        "question": "你想重点讲哪几个方向？",
        "multi": True,
        "options": [{"label": item, "value": item} for item in options],
    }


def _normalize_video_questions(questions: list[dict] | None, topic: str = "") -> list[dict]:
    """Make question ids/values useful for downstream prompt constraints."""
    if not isinstance(questions, list):
        return []

    normalized: list[dict] = []
    has_focus = False
    has_depth = False

    def _depth_value(label: str, value: str) -> str:
        raw = f"{label} {value}".lower()
        if any(k in raw for k in ("overview", "概览", "核心", "快速", "极速")):
            return "overview"
        if any(k in raw for k in ("deep", "深入", "详解", "推导", "案例")):
            return "deep"
        if any(k in raw for k in ("standard", "标准", "原理", "应用")):
            return "standard"
        return value or "standard"

    for idx, item in enumerate(questions[:3]):
        if not isinstance(item, dict):
            continue
        q = dict(item)
        qid = str(q.get("id") or "").strip().lower()
        qtext = str(q.get("question") or "").strip()
        is_focus = qid.startswith("focus") or "聚焦" in qtext or "重点" in qtext or "方向" in qtext
        is_depth = qid == "depth" or "深度" in qtext or "多深" in qtext or "层次" in qtext

        if is_focus and not has_focus:
            q["id"] = "focus"
            q["multi"] = True
            has_focus = True
        elif is_depth and not has_depth:
            q["id"] = "depth"
            q["multi"] = False
            has_depth = True
        else:
            continue

        options = []
        for opt in q.get("options") or []:
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("label") or opt.get("value") or "").strip()
            if not label:
                continue
            value = str(opt.get("value") or label).strip()
            if q["id"] == "focus":
                value = label
            elif q["id"] == "depth":
                value = _depth_value(label, value)
            options.append({"label": label, "value": value})
        q["options"] = options
        if q["options"]:
            normalized.append(q)

    if not has_focus:
        normalized.insert(0, _default_focus_question(topic))
    if not has_depth:
        normalized.append(_default_depth_question())

    return normalized


async def _generate_questions_from_topic(topic: str, portrait_context: str, user_id: int = 0) -> list[dict]:
    """无资源时，基于话题常识生成问题"""
    prompt = fill_prompt(
        load_prompt("presentation/questions_topic_only"),
        topic=topic,
        portrait_context=portrait_context or "暂无画像",
    )

    try:
        resp = await asyncio.wait_for(llm.ainvoke(prompt, priority="high", user_id=user_id, pool="leader"), timeout=8)
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        questions_data = json.loads(raw)
        return questions_data.get("questions", [])
    except (asyncio.TimeoutError, json.JSONDecodeError):
        logger.warning("AI 问题（话题模式）生成超时或解析失败")
        return []
    except Exception:
        logger.exception("AI 问题（话题模式）生成失败")
        return []


# ─── 内容裁剪 ───

def _parse_focus_values(answers: dict) -> set[str]:
    """从 answers 中提取 focus 关键词"""
    keywords: set[str] = set()
    for key, val in answers.items():
        if key.startswith("focus") or key == "focus":
            if isinstance(val, list):
                for v in val:
                    keywords.update(_extract_keywords(v))
            elif isinstance(val, str):
                keywords.update(_extract_keywords(val))
    return keywords


def _extract_keywords(val: str) -> set[str]:
    """从 value 中拆词，如 'supervised_learning' → {'supervised', 'learning'}"""
    # 同时也保留原始值做子串匹配
    kw = {val.lower(), val.lower().replace("_", " "), val.lower().replace("_", "")}
    # 拆词
    for part in val.lower().replace("_", " ").split():
        if len(part) > 1:
            kw.add(part)
    return kw


def _crop_document(record, focus_vals: set[str], depth: str):
    """裁剪文档内容：只保留匹配的章节，并控制密度"""
    if not record:
        return None
    content = record.content or ""
    raw_parts = re.split(r"\n(?=## )", content.strip())
    if len(raw_parts) <= 1:
        raw_parts = re.split(r"\n(?=# )", content.strip())

    if not focus_vals:
        new_content = _trim_content_depth(content, depth)
        return _make_cropped_copy(record, new_content)

    kept: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        title = part.split("\n")[0].lstrip("#").strip().lower()
        text = part.lower()
        if any(kw in title or kw in text for kw in focus_vals):
            kept.append(part)

    if not kept:
        return record

    new_content = "\n\n".join(kept)
    if depth == "overview":
        new_content = _trim_content_depth(new_content, depth)
    return _make_cropped_copy(record, new_content)


def _crop_ppt(record, focus_vals: set[str], depth: str):
    """裁剪 PPT 内容：只保留匹配的幻灯片"""
    if not record:
        return None
    content = record.content or ""
    slides = re.split(r"\n---\n", content.strip())

    if not focus_vals:
        new_content = _trim_content_depth(content, depth, is_ppt=True)
        return _make_cropped_copy(record, new_content)

    kept: list[str] = []
    for slide in slides:
        slide = slide.strip()
        if not slide:
            continue
        slide_lower = slide.lower()
        if any(kw in slide_lower for kw in focus_vals):
            kept.append(slide)

    if not kept:
        return record

    new_content = "\n---\n".join(kept)
    if depth == "overview":
        new_content = _trim_content_depth(new_content, depth, is_ppt=True)
    return _make_cropped_copy(record, new_content)


def _make_cropped_copy(record, new_content: str):
    """创建裁剪后的轻量副本，不污染 ORM 对象"""
    return SimpleNamespace(
        id=record.id,
        content=new_content,
        resource_type=record.resource_type,
        topic=record.topic,
    )


def _trim_content_depth(content: str, depth: str, is_ppt: bool = False) -> str:
    """概览模式：每段只保留标题 + 前 2 个 bullet"""
    if depth != "overview":
        return content
    sep = "\n---\n" if is_ppt else "\n\n"
    blocks = content.split(sep)
    trimmed: list[str] = []
    for block in blocks:
        lines = block.strip().split("\n")
        kept_lines = []
        bullet_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                if bullet_count < 2:
                    kept_lines.append(line)
                    bullet_count += 1
            else:
                kept_lines.append(line)
        trimmed.append("\n".join(kept_lines))
    return sep.join(trimmed)


_sse_queues: dict[int, list[asyncio.Queue]] = {}
_sse_forward_tasks: dict[int, tuple[asyncio.Task, list]] = {}


def _subscribe_sse(presentation_id: int) -> asyncio.Queue:
    """订阅视频 SSE（返回 asyncio.Queue，兼容现有路由）。
    同时在 redis_client 注册订阅，跨进程消息通过转发器到达本地队列。"""
    q = asyncio.Queue()
    _sse_queues.setdefault(presentation_id, []).append(q)

    # 注册 redis_client 统一订阅，使跨进程消息能转发到此队列
    _rq = subscribe_sse(f"pres:{presentation_id}")

    async def _forward_loop():
        """将 redis_client 订阅队列中的消息转发到 asyncio.Queue"""
        try:
            while q in _sse_queues.get(presentation_id, []):
                while _rq:
                    msg = _rq.pop(0)
                    q.put_nowait(msg)
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("SSE 转发结束 presentation_id=%s", presentation_id)

    _sse_forward_tasks[id(q)] = (asyncio.ensure_future(_forward_loop()), _rq)
    return q


def _unsubscribe_sse(presentation_id: int, q: asyncio.Queue):
    queues = _sse_queues.get(presentation_id, [])
    if q in queues:
        queues.remove(q)
    if not queues:
        _sse_queues.pop(presentation_id, None)
    # 取消当前连接的转发任务并清理 redis_client 订阅
    task_info = _sse_forward_tasks.pop(id(q), None)
    if not task_info:
        return
    task, redis_queue = task_info
    unsubscribe_sse(f"pres:{presentation_id}", redis_queue)
    if task and not task.done():
        task.cancel()


async def _notify_sse(presentation_id: int, data: dict):
    """统一走 redis_client 通知（本地队列 + Redis Pub/Sub + Stream）"""
    await _redis_notify_sse(f"pres:{presentation_id}", data)


_VIDEO_PROGRESS_AGENT_MAP = {
    "portrait_intro": ("leader", "LeaderAgent", "leader", None),
    "generate_document": ("executor:document", "文档生成智能体", "executor", "document"),
    "generate_ppt": ("executor:ppt", "PPT生成智能体", "executor", "ppt"),
    "build_intro": ("executor:document", "文档生成智能体", "executor", "document"),
    "build_ppt": ("executor:ppt", "PPT生成智能体", "executor", "ppt"),
    "generate_resources": ("executor", "视频生成智能体", "executor", None),
    "render_html": ("saver", "ResourceService", "saver", None),
    "audio": ("executor:audio", "TTS生成智能体", "executor", None),
    "done": ("complete", "完成", "complete", None),
    "error": ("complete", "生成失败", "complete", None),
}


def _video_agent_status(step: str, status: str, phase: str) -> str:
    value = str(status or "").lower()
    if value in {"error", "failed"} or step == "error":
        return "failed"
    if step == "done" or value == "done":
        return "done"
    if phase == "saver":
        return "saving"
    return "running"


def _push_agent_progress(
    presentation_id: int,
    step: str,
    label: str,
    status: str = "running",
    elapsed_ms: int | None = None,
    *,
    current: int | None = None,
    total: int | None = None,
):
    """给前端智能体工作流面板推送统一事件。"""
    agent_id, agent_name, phase, resource_type = _VIDEO_PROGRESS_AGENT_MAP.get(
        step,
        ("executor", "视频生成智能体", "executor", None),
    )
    event = {
        "type": "agent_event",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "phase": phase,
        "status": _video_agent_status(step, status, phase),
        "message": label,
        "elapsed_ms": elapsed_ms,
        "source_event_type": "video_progress",
        "step": step,
    }
    if resource_type:
        event["resource_type"] = resource_type
    if current is not None:
        event["current"] = current
    if total is not None:
        event["total"] = total
    asyncio.ensure_future(_redis_notify_sse(f"pres:{presentation_id}", event))


def _push_progress(presentation_id: int, step: str, label: str, status: str = "running", elapsed_ms: int | None = None):
    """推送生成进度（统一走 redis_client，异步 fire-and-forget）"""
    event = {"type": "progress", "step": step, "label": label, "status": status, "elapsed_ms": elapsed_ms}
    asyncio.ensure_future(_redis_notify_sse(f"pres:{presentation_id}", event))
    _push_agent_progress(presentation_id, step, label, status, elapsed_ms)



# ═══════════════════════════════════════════════
#  对外 API
# ═══════════════════════════════════════════════

async def generate(topic: str, user_id: int, voice: str = DEFAULT_VIDEO_VOICE,
                  chapters: list[str] | None = None, answers: dict | None = None,
                  chat_group_id: int = 0, video_mode: bool = False,
                  background: bool = False, save_history: bool = True) -> dict:
    """创建视频。background=True 立即返回 {id, status:'generating'}，进度走 SSE；background=False 同步等完整结果"""
    import time as _time
    from backend.src.models.usermodel import User

    user = await User.filter(id=user_id).first()
    if not user:
        raise ServiceError("用户不存在")

    # 立即创建记录，拿到 ID 用于 SSE 进度推送
    record = await Video.create(user=user, topic=topic, status="generating")
    pid = record.id
    _t_total = _time.perf_counter()

    async def _do_generate():
        nonlocal chat_group_id
        try:
            # — 先准备章节骨架与 TTS 调度器：资源流一产出就能立刻补音频 —
            chapters_list: list[dict] = []
            audio_tasks: list[dict] = []
            audio_background_tasks: list[asyncio.Task] = []
            ppt_streamed_sections: set[int] = set()
            doc_chapter_idx: int | None = None
            ppt_base_idx: int | None = None
            doc_ready = False
            ppt_full_ready = False

            async def _persist_partial_chapters(status: str = "ready"):
                record.chapters_json = json.dumps(chapters_list, ensure_ascii=False)
                record.total_duration_ms = sum(c.get("total_duration_ms", 0) for c in chapters_list)
                record.status = status
                await record.save()
                await _notify_sse(pid, {
                    "status": status,
                    "chapters": len(chapters_list),
                    "file_url": _versioned_presentation_url(record.file_url),
                })

            def _schedule_chapter_audio(task: dict):
                if not VIDEO_AUDIO_AUTO_GENERATE:
                    return
                handle = asyncio_create_task(
                    _add_audio_to_presentation(
                        record.id, topic, user_id, voice, chapters_list, [task],
                        notify_complete=False,
                    )
                )
                if handle:
                    audio_background_tasks.append(handle)

            def _pending_chapter(title: str, chapter_type: str = "ppt") -> dict:
                return {
                    "type": chapter_type,
                    "title": title,
                    "slides": [],
                    "total_duration_ms": 0,
                    "is_audio_ready": False,
                    "pending": True,
                }

            async def _add_or_replace_chapter(idx: int, chapter: dict, task: dict):
                if 0 <= idx < len(chapters_list):
                    chapters_list[idx] = chapter
                else:
                    while len(chapters_list) < idx:
                        chapters_list.append(_pending_chapter("生成中"))
                    chapters_list.append(chapter)
                audio_tasks.append(task)
                await _persist_partial_chapters()
                _schedule_chapter_audio(task)

            async def _add_document_chapter(doc_resource):
                nonlocal doc_ready
                if doc_ready:
                    return
                doc_ready = True
                t_doc = _time.perf_counter()
                _push_progress(pid, "build_intro", "正在构建文档章节…", "running")
                if answers:
                    doc_resource = _crop_document(
                        doc_resource,
                        _parse_focus_values(answers),
                        (answers or {}).get("depth", "standard"),
                    )
                ch = _build_intro_skeleton(doc_resource)
                idx = doc_chapter_idx if doc_chapter_idx is not None else len(chapters_list)
                task = {"chapter_idx": idx, "resource": doc_resource}
                await _add_or_replace_chapter(idx, ch, task)
                _push_progress(pid, "build_intro", "文档章节构建完毕", "done", int((_time.perf_counter() - t_doc) * 1000))

            def _ensure_ppt_slots(section_total: int):
                nonlocal ppt_base_idx
                total = max(1, int(section_total or 1))
                if ppt_base_idx is None:
                    ppt_base_idx = len(chapters_list)
                while len(chapters_list) < ppt_base_idx + total:
                    offset = len(chapters_list) - ppt_base_idx + 1
                    chapters_list.append(_pending_chapter(f"PPT 第 {offset} 章生成中", "ppt"))

            async def _add_ppt_section(section_idx: int, section_title: str, section_total: int, content: str):
                if section_idx in ppt_streamed_sections or not str(content or "").strip():
                    return
                ppt_streamed_sections.add(section_idx)
                _ensure_ppt_slots(section_total or (section_idx + 1))
                from types import SimpleNamespace as _SN
                ppt_resource = _SN(
                    id=record.id * 1000 + 100 + section_idx,
                    topic=section_title or f"{topic} 第 {section_idx + 1} 章",
                    resource_type="ppt",
                    content=content,
                )
                ch = _build_ppt_skeleton(ppt_resource, plain=video_mode)
                ch["title"] = section_title or ch.get("title") or f"PPT 第 {section_idx + 1} 章"
                ch["section_idx"] = section_idx
                idx = (ppt_base_idx or 0) + section_idx
                task = {"chapter_idx": idx, "resource": ppt_resource}
                await _add_or_replace_chapter(idx, ch, task)
                logger.info("[视频] PPT章节已进入TTS record=%d section=%d/%d title=%s", record.id, section_idx + 1, section_total, ch["title"])

            async def _add_full_ppt_chapter(ppt_resource):
                nonlocal ppt_full_ready
                if ppt_full_ready or ppt_streamed_sections:
                    return
                ppt_full_ready = True
                t_ppt = _time.perf_counter()
                _push_progress(pid, "build_ppt", "正在构建PPT章节…", "running")
                if answers:
                    ppt_resource = _crop_ppt(
                        ppt_resource,
                        _parse_focus_values(answers),
                        (answers or {}).get("depth", "standard"),
                    )
                ch = _build_ppt_skeleton(ppt_resource, plain=video_mode)
                idx = len(chapters_list)
                task = {"chapter_idx": idx, "resource": ppt_resource}
                await _add_or_replace_chapter(idx, ch, task)
                _push_progress(pid, "build_ppt", "PPT章节构建完毕", "done", int((_time.perf_counter() - t_ppt) * 1000))

            async def _on_resource_complete(resource_type: str, resource):
                if resource_type == "document":
                    await _add_document_chapter(resource)
                    _push_agent_progress(pid, "generate_document", "文档资料生成完毕", "done")
                elif resource_type == "ppt":
                    await _add_full_ppt_chapter(resource)
                    _push_agent_progress(pid, "generate_ppt", "PPT资料生成完毕", "done")

            async def _on_ppt_section_complete(section_idx: int, section_title: str, section_total: int, content: str):
                await _notify_sse(pid, {
                    "type": "stream_slide",
                    "resource_type": "ppt",
                    "file_type": "ppt",
                    "section_idx": section_idx,
                    "slide_idx": 0,
                    "section_title": section_title,
                    "content": content,
                    "current": section_idx + 1,
                    "total": section_total,
                    "progress_msg": f"PPT 已生成 {section_idx + 1}/{section_total} 章节",
                })
                await _add_ppt_section(section_idx, section_title, section_total, content)

            # — 第1步：个性化引入，生成完马上开始 TTS —
            t0 = _time.perf_counter()
            _push_progress(pid, "portrait_intro", "正在生成个性化引入…", "running")
            portrait_intro = await _get_prewarmed_portrait_intro(topic, user_id, voice or DEFAULT_VIDEO_VOICE)
            if not portrait_intro:
                portrait_intro = await _build_portrait_intro(topic, user)
            _push_progress(pid, "portrait_intro", "个性化引入生成完毕", "done", int((_time.perf_counter() - t0) * 1000))

            if portrait_intro:
                from types import SimpleNamespace as _SN
                intro_raw = portrait_intro.pop("_raw_text", "")
                intro_resource = _SN(id=0, content=intro_raw, resource_type="document")
                chapters_list.append(portrait_intro)
                task = {"chapter_idx": len(chapters_list) - 1, "resource": intro_resource}
                intro_audio_ready = bool(portrait_intro.get("is_audio_ready"))
                if intro_audio_ready:
                    logger.info("[视频] 画像引入使用预热音频 record=%d topic=%s", record.id, topic[:60])
                else:
                    audio_tasks.append(task)
                await _persist_partial_chapters()
                if not intro_audio_ready:
                    _schedule_chapter_audio(task)

            # 预留文档位置，PPT 章节可以先完成但不会把播放顺序顶乱。
            doc_chapter_idx = len(chapters_list)
            chapters_list.append(_pending_chapter("文档章节生成中", "intro"))
            await _persist_partial_chapters("generating")

            # — 第2步：流式重新生成本次视频资料；PPT 单章完成就立刻 TTS —
            t0 = _time.perf_counter()
            _push_progress(pid, "generate_resources", "正在重新生成视频资料…", "running")
            _push_agent_progress(pid, "generate_document", "正在生成文档资料…", "running")
            await _notify_sse(pid, {
                "type": "stream_start",
                "resource_type": "ppt",
                "file_type": "ppt",
                "progress_msg": "PPT 流式生成已启动",
            })
            doc, mindmap_data, ppt_data = await _generate_fresh_video_resources(
                topic,
                user_id,
                record.id,
                answers,
                chat_group_id,
                on_resource_complete=_on_resource_complete,
                on_ppt_section_complete=_on_ppt_section_complete,
            )
            if not any([doc, mindmap_data, ppt_data]) and not ppt_streamed_sections:
                raise ServiceError(f"话题「{topic}」视频资料生成失败，请稍后重试")
            if doc and not doc_ready:
                await _add_document_chapter(doc)
                _push_agent_progress(pid, "generate_document", "文档资料生成完毕", "done")
            if ppt_data and not ppt_streamed_sections and not ppt_full_ready:
                await _add_full_ppt_chapter(ppt_data)
                _push_agent_progress(pid, "generate_ppt", "PPT资料生成完毕", "done")
            if not doc_ready and doc_chapter_idx is not None and 0 <= doc_chapter_idx < len(chapters_list):
                chapters_list[doc_chapter_idx] = {
                    **chapters_list[doc_chapter_idx],
                    "title": "文档章节暂不可用",
                    "is_audio_ready": True,
                    "pending": False,
                }
                await _persist_partial_chapters()
                _push_agent_progress(pid, "generate_document", "文档资料暂不可用，已跳过文档章节", "done")
            if ppt_streamed_sections:
                total = max(len(ppt_streamed_sections), len(chapters_list) - (ppt_base_idx or len(chapters_list)), 1)
                _push_agent_progress(
                    pid,
                    "generate_ppt",
                    "PPT资料生成完毕",
                    "done",
                    current=len(ppt_streamed_sections),
                    total=total,
                )
            _push_progress(pid, "generate_resources", "视频资料重新生成完毕", "done", int((_time.perf_counter() - t0) * 1000))

            # — 第6步：渲染 HTML —
            file_url = ""
            if not video_mode:
                t0 = _time.perf_counter()
                _push_progress(pid, "render_html", "正在渲染视频…", "running")
                VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
                html = _render_html(topic, chapters_list, _build_audio_segments(chapters_list), template_path=None)
                filename = f"{_safe_filename(topic)}_{uuid.uuid4().hex[:8]}.html"
                file_path = VIDEOS_DIR / filename
                async with _get_video_audio_flush_lock(record.id):
                    _write_text_atomic(file_path, html)
                    _remember_template_version(file_path, PRESENTATION_TEMPLATE_VERSION)
                file_url = f"/static/presentations/{filename}"
                _push_progress(pid, "render_html", "视频渲染完毕", "done", int((_time.perf_counter() - t0) * 1000))

            # 更新记录
            record.chapters_json = json.dumps(chapters_list, ensure_ascii=False)
            record.total_duration_ms = sum(c.get("total_duration_ms", 0) for c in chapters_list)
            record.status = "ready"
            if file_url:
                record.file_url = file_url
            await record.save()

            # 保存到聊天历史
            if save_history:
                cgid = chat_group_id if chat_group_id and chat_group_id > 0 else await allocate_chat_group_id(user_id)
                chat_group_id = cgid
                try:
                    req_text = topic
                    if answers:
                        selected = []
                        for v in answers.values():
                            if isinstance(v, list):
                                selected.extend(str(x) for x in v)
                            elif v:
                                selected.append(str(v))
                        if selected:
                            req_text = f"{topic}（已选择：{' / '.join(selected)}）"
                    res_json = json.dumps({
                        "type": "presentation",
                        "id": record.id,
                        "topic": topic,
                        "file_url": _versioned_presentation_url(record.file_url),
                        "status": "ready",
                        "_video_hint": "学习视频已生成，可立即查看",
                    }, ensure_ascii=False)
                    await ChatHistory.create(user=user, chat_group_id=cgid, req=req_text, res=res_json)
                except Exception:
                    logger.exception("保存视频到聊天历史失败")

            # 推送通知
            try:
                await Notification.create(
                    type="resource",
                    title="视频已生成",
                    content=f"「{topic}」视频已生成，共 {len(chapters_list)} 章，可立即查看（音频后台补充中）",
                    target_url=f"/presentation?id={record.id}",
                    target_user_id=user_id,
                )
            except Exception:
                logger.exception("视频通知推送失败")

            # 后台补音频
            if VIDEO_AUDIO_AUTO_GENERATE and audio_tasks and not audio_background_tasks:
                asyncio_create_task(_add_audio_to_presentation(record.id, topic, user_id, voice, chapters_list, audio_tasks))
            elif audio_background_tasks:
                async def _notify_audio_tasks_done():
                    await asyncio.gather(*audio_background_tasks, return_exceptions=True)
                    audio_ready_chapters = sum(1 for c in chapters_list if c.get("is_audio_ready"))
                    audio_segments = len(_build_audio_segments(chapters_list))
                    await _notify_sse(pid, {
                        "type": "audio_progress",
                        "status": "all_ready",
                        "audio_ready_chapters": audio_ready_chapters,
                        "audio_segments": audio_segments,
                    })
                    _push_agent_progress(
                        pid,
                        "audio",
                        "TTS 音频全部就绪",
                        "done",
                        current=audio_ready_chapters,
                        total=len(chapters_list),
                    )
                    try:
                        await Notification.create(
                            type="resource",
                            title="视频制作完成",
                            content=f"「{topic}」视频音频已补齐，共 {len(chapters_list)} 章，可播放",
                            target_url=f"/presentation?id={record.id}",
                            target_user_id=user_id,
                        )
                    except Exception:
                        logger.debug("[视频] audio complete notification failed", exc_info=True)

                asyncio_create_task(_notify_audio_tasks_done())

            total_ms = int((_time.perf_counter() - _t_total) * 1000)
            chapter_names = [c.get("title", "") for c in chapters_list]
            _push_progress(pid, "done", f"视频生成完毕，共 {len(chapters_list)} 章（{' → '.join(chapter_names)}）", "done", total_ms)
            await _notify_sse(pid, {
                "status": "ready",
                "file_url": _versioned_presentation_url(file_url),
                "id": record.id,
                "chapters": len(chapters_list),
            })
            logger.info("[视频] 骨架生成完成 topic=%s record=%d chapters=%d 耗时=%.1fs",
                        topic, record.id, len(chapters_list), _time.perf_counter() - _t_total)

            return {
                "id": record.id,
                "file_url": _versioned_presentation_url(file_url),
                "status": "ready",
                "template_version": VIDEO_TEMPLATE_VERSION if video_mode else PRESENTATION_TEMPLATE_VERSION,
                "message": "视频已生成，音频在后台补充中",
            }

        except Exception as e:
            logger.exception("视频生成失败")
            record.status = "failed"
            record.error_message = str(e)
            await record.save()
            _push_progress(pid, "error", str(e), "error")
            await _notify_sse(pid, {"status": "failed", "error": str(e)})
            raise

    if background:
        asyncio_create_task(_do_generate())
        return {
            "id": record.id,
            "status": "generating",
            "template_version": VIDEO_TEMPLATE_VERSION if video_mode else PRESENTATION_TEMPLATE_VERSION,
            "message": "视频生成中，请通过 SSE 获取进度",
        }
    else:
        return await _do_generate()


async def get_presentation(presentation_id: int, user_id: int) -> dict | None:
    """查询视频状态"""
    record = await Video.filter(id=presentation_id, user_id=user_id).first()
    if not record:
        return None

    chapters = []
    if record.chapters_json:
        chapters = json.loads(record.chapters_json)

    return {
        "id": record.id,
        "topic": record.topic,
        "status": record.status,
        "file_url": _versioned_presentation_url(record.file_url),
        "template_version": PRESENTATION_TEMPLATE_VERSION,
        "chapters": chapters,
        "chapter_count": len(chapters),
        "total_duration_ms": record.total_duration_ms,
        "error_message": record.error_message,
        "created_at": str(record.created_at),
    }


async def list_presentations(user_id: int) -> list[dict]:
    """列出用户所有视频"""
    records = await Video.filter(user_id=user_id).order_by("-created_at").all()
    return [
        {
            "id": r.id,
            "topic": r.topic,
            "status": r.status,
            "chapter_count": len(json.loads(r.chapters_json)) if r.chapters_json else 0,
            "total_duration_ms": r.total_duration_ms,
            "file_url": _versioned_presentation_url(r.file_url),
            "template_version": PRESENTATION_TEMPLATE_VERSION,
            "created_at": str(r.created_at),
        }
        for r in records
    ]


async def delete_presentation(presentation_id: int, user_id: int) -> bool:
    """删除视频和 HTML 文件"""
    record = await Video.filter(id=presentation_id, user_id=user_id).first()
    if not record:
        return False

    if record.file_url:
        fp = _presentation_file_path(record.file_url)
        if fp and fp.exists():
            fp.unlink()

    await record.delete()
    return True


async def _add_audio_to_presentation(record_id: int, topic: str, user_id: int, voice: str,
                                      chapters: list[dict], audio_tasks: list[dict],
                                      notify_complete: bool = True):
    """骨架已出，优先复用旁白 DB 缓存，无缓存时才逐 slide 生成音频"""
    import os as _os
    import time as _time
    from backend.src.models.narration_model import Narration
    from backend.src.service.narration.service import _generate_tts, TTS_GLOBAL_CONCURRENCY, TTS_PER_USER_CONCURRENCY
    from backend.src.utils.tts_utils import parse_by_type as _parse_by_type

    _t_start = _time.perf_counter()

    record = await Video.filter(id=record_id).first()
    if not record:
        return

    _flush_lock = _get_video_audio_flush_lock(record_id)
    logger.info(
        "[Video-TTS] limits record=%d user=%s per_user=%d global=%d chapter=%d slide=%d",
        record_id, user_id, TTS_PER_USER_CONCURRENCY, TTS_GLOBAL_CONCURRENCY, AUDIO_CHAPTER_CONCURRENCY, AUDIO_SLIDE_CONCURRENCY,
    )

    logger.info(
        "[视频] TTS任务启动 record=%d chapters=%d tasks=%d chapter_concurrency=%d slide_concurrency=%d global_tts=%d notify=%s",
        record_id, len(chapters), len(audio_tasks), AUDIO_CHAPTER_CONCURRENCY, AUDIO_SLIDE_CONCURRENCY, TTS_GLOBAL_CONCURRENCY, notify_complete,
    )

    _TTS_CACHE = AUDIO_DIR / "_cache"
    _TTS_CACHE.mkdir(parents=True, exist_ok=True)

    async def _tts_one_slide(text: str, resource_id: int, slide_idx: int) -> dict | None:
        """生成单张 slide 的 TTS 音频，返回 {audio_url, duration_ms, word_timestamps} 或 None"""
        if not text:
            return None
        cache_key = _tts_cache_key(text, voice)
        base_dir = AUDIO_DIR / str(resource_id)
        base_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(base_dir / f"{cache_key}.mp3")
        json_path = str(base_dir / f"{cache_key}.json")
        audio_url = f"/static/audio/{resource_id}/{cache_key}.mp3"

        global_mp3 = str(_TTS_CACHE / f"{cache_key}.mp3")
        global_json = str(_TTS_CACHE / f"{cache_key}.json")

        if not (_os.path.exists(output_path) and _os.path.exists(json_path)):
            if _os.path.exists(global_mp3) and _os.path.exists(global_json):
                import shutil as _shutil
                _shutil.copy2(global_mp3, output_path)
                _shutil.copy2(global_json, json_path)

        if _os.path.exists(output_path) and _os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    word_timestamps = json.load(f)
                dur = _real_duration_from_timestamps(word_timestamps, int(len(text) / 4 * 1000))
                logger.info("[视频] TTS 缓存命中 resource=%d slide=%d text_len=%d", resource_id, slide_idx, len(text))
                return {"audio_url": audio_url, "duration_ms": dur, "word_timestamps": word_timestamps}
            except (json.JSONDecodeError, IOError):
                logger.warning(
                    "[视频] TTS 缓存读取失败，重新生成 resource=%d slide=%d json=%s",
                    resource_id, slide_idx, json_path,
                    exc_info=True,
                )

        t0 = _time.perf_counter()
        word_timestamps = await _generate_tts(text, voice, output_path, user_id=user_id)
        if word_timestamps is None:
            return None

        t_cost = _time.perf_counter() - t0
        logger.info("[视频] TTS 生成耗时 resource=%d slide=%d text_len=%d cost=%.1fs", resource_id, slide_idx, len(text), t_cost)

        dur = _real_duration_from_timestamps(word_timestamps, int(len(text) / 4 * 1000))
        return {"audio_url": audio_url, "duration_ms": dur, "word_timestamps": word_timestamps}

    async def _flush_audio_state(ch_idx: int | None = None, slide_idx: int | None = None, status: str = "running"):
        partial_segments = _build_audio_segments(chapters)
        force_flush = status != "slide_ready"
        audio_ready_chapters = sum(1 for c in chapters if c.get("is_audio_ready"))
        async with _flush_lock:
            if _should_flush_video_html(record_id, force=force_flush):
                await _flush(record, topic, chapters, "ready", segments=partial_segments)
        await _notify_sse(record_id, {
            "type": "audio_progress",
            "status": status,
            "chapter_idx": ch_idx,
            "slide_idx": slide_idx,
            "audio_ready_chapters": audio_ready_chapters,
            "audio_segments": len(partial_segments),
        })
        if status == "chapter_ready":
            _push_agent_progress(
                record_id,
                "audio",
                f"TTS 音频已完成 {audio_ready_chapters}/{len(chapters)} 章",
                "running",
                current=audio_ready_chapters,
                total=len(chapters),
            )

    async def _process_chapter(task: dict, frontload: bool = False):
        t_ch_start = _time.perf_counter()
        res = task["resource"]
        ch_idx = task["chapter_idx"]
        ch = chapters[ch_idx]
        resource_id = res.id if hasattr(res, 'id') else 0
        content = res.content if hasattr(res, 'content') else (res.get('content') if isinstance(res, dict) else '')
        resource_type = res.resource_type if hasattr(res, 'resource_type') else (res.get('resource_type', 'document') if isinstance(res, dict) else 'document')

        # 优先复用旁白 DB 缓存（_pre_generate_narration 在资源生成时已写入）
        narration = await Narration.filter(resource_id=resource_id, voice=voice).first()
        if narration and narration.slides_json:
            if resource_type == "document":
                _patch_intro_audio(ch, {"sections": narration.slides_json})
            elif resource_type == "ppt":
                _patch_ppt_audio(ch, {"sections": narration.slides_json})
            elif resource_type == "mindmap":
                _patch_mindmap_audio(ch, {"sections": narration.slides_json})
            ch["is_audio_ready"] = True
            t_flush = _time.perf_counter()
            await _flush_audio_state(ch_idx, status="chapter_ready")
            logger.info("[视频] 复用旁白缓存 chapter=%d resource=%d cost=%.1fs flush=%.1fs",
                        ch_idx, resource_id, _time.perf_counter() - t_ch_start, _time.perf_counter() - t_flush)
            return

        # 无旁白缓存 → 逐 slide 生成 TTS
        sections = _parse_by_type(content, resource_type)
        if not sections:
            ch["is_audio_ready"] = True
            await _flush_audio_state(ch_idx, status="chapter_ready")
            return

        slides = ch.get("slides", [])
        total_slides = len(sections)
        slide_sem = asyncio.Semaphore(AUDIO_SLIDE_CONCURRENCY)

        async def _one_slide(i: int, section: dict):
            text = section.get("text", "")
            async with slide_sem:
                result = await _tts_one_slide(text, resource_id, i)
            if result and i < len(slides):
                slides[i]["audio_url"] = result["audio_url"]
                slides[i]["duration_ms"] = result["duration_ms"]
                slides[i]["word_timestamps"] = result.get("word_timestamps", [])
            done_count = sum(1 for s in slides if s.get("audio_url"))
            ch["total_duration_ms"] = sum(s.get("duration_ms", 0) for s in slides)
            ch["audio_slide_count"] = done_count
            logger.info("[视频] slide 音频进度 chapter=%d %d/%d record=%d", ch_idx, done_count, total_slides, record_id)

        start_idx = 0
        if frontload:
            start_idx = min(AUDIO_FRONTLOAD_SLIDES, len(sections))
            for i in range(start_idx):
                await _one_slide(i, sections[i])
                await _flush_audio_state(ch_idx, i, status="slide_ready")

        rest = [(i, s) for i, s in enumerate(sections[start_idx:], start=start_idx)]
        if rest:
            await asyncio.gather(*(_one_slide(i, s) for i, s in rest), return_exceptions=True)
        ch["is_audio_ready"] = True
        t_flush = _time.perf_counter()
        await _flush_audio_state(ch_idx, status="chapter_ready")
        logger.info("[视频] 音频已补 chapter=%d type=%s cost=%.1fs flush=%.1fs record=%d",
                    ch_idx, ch.get("type"), _time.perf_counter() - t_ch_start, _time.perf_counter() - t_flush, record_id)

    try:
        ordered_tasks = sorted(audio_tasks, key=lambda t: int(t.get("chapter_idx", 0)))
        front_tasks = ordered_tasks[:AUDIO_FRONTLOAD_CHAPTERS]
        rest_tasks = ordered_tasks[AUDIO_FRONTLOAD_CHAPTERS:]

        chapter_sem = asyncio.Semaphore(AUDIO_CHAPTER_CONCURRENCY)

        async def _run_background_chapter(task: dict):
            async with chapter_sem:
                await _process_chapter(task, frontload=False)

        background_runs = [_run_background_chapter(t) for t in rest_tasks]
        front_runs = [_process_chapter(t, frontload=True) for t in front_tasks]
        if front_runs or background_runs:
            await asyncio.gather(*(front_runs + background_runs), return_exceptions=True)

        segments = _build_audio_segments(chapters)
        async with _flush_lock:
            await _flush(record, topic, chapters, "ready", segments=segments)
        total_cost = _time.perf_counter() - _t_start
        audio_ready = sum(1 for c in chapters if c.get("is_audio_ready"))
        total_segments = len(segments)
        total_dur = sum(s.get("duration_ms", 0) for s in segments)
        logger.info("[视频] 完成 record=%d chapters=%d/%d segments=%d duration=%dms cost=%.1fs",
                    record_id, audio_ready, len(chapters), total_segments, total_dur, total_cost)
        if notify_complete:
            await Notification.create(
                type="resource",
                title="视频制作完成",
                content=f"「{topic}」视频已生成，共 {len(chapters)} 章，可播放",
                target_url=f"/presentation?id={record_id}",
                target_user_id=user_id,
            )

    except Exception as e:
        logger.exception("[视频] 生成失败 record=%d", record_id)
        async with _flush_lock:
            await _flush(record, topic, chapters, "failed")
        record = await Video.filter(id=record_id).first()
        if record:
            record.error_message = str(e)[:500]
            await record.save()
        _push_agent_progress(record_id, "audio", f"TTS 生成失败：{str(e)[:100]}", "failed")
        await _notify_sse(record_id, {"status": "failed", "error": str(e)[:200]})
        await Notification.create(
            type="resource",
            title="视频生成失败",
            content=f"「{topic}」视频生成失败：{str(e)[:100]}",
            target_url="/resource",
            target_user_id=user_id,
        )


def _build_audio_segments(chapters: list[dict]) -> list[dict]:
    """从章节中提取播放列表（每 slide 独立音频，不合并）"""
    segments: list[dict] = []
    for ci, ch in enumerate(chapters):
        if ch.get("slides"):
            for si, slide in enumerate(ch["slides"]):
                if slide.get("audio_url"):
                    segments.append({
                        "url": slide["audio_url"],
                        "duration_ms": slide.get("duration_ms", 0),
                        "chapter": ci,
                        "slide": si,
                    })
        if ch.get("audio_segments"):
            for seg in ch["audio_segments"]:
                if seg.get("audio_url"):
                    segments.append({
                        "url": seg["audio_url"],
                        "duration_ms": seg.get("duration_ms", 0),
                        "chapter": ci,
                        "slide": 0,
                    })
    return segments


def _real_duration_from_timestamps(word_timestamps: list[dict], fallback_ms: int) -> int:
    """从词级时间戳计算真实音频时长（最后一个词的 offset + duration），无时间戳时回退到估算值"""
    if word_timestamps:
        last = word_timestamps[-1]
        real = last.get("offset_ms", 0) + last.get("duration_ms", 0)
        if real > 0:
            return real
    return fallback_ms


async def _flush(record, topic: str, chapters: list, status: str, segments: list[dict] | None = None):
    """更新 HTML 文件（如有）+ DB + SSE 通知"""
    record = await Video.filter(id=record.id).first()
    if not record:
        return

    file_path = _presentation_file_path(record.file_url)
    if file_path and file_path.exists():
        version = _detect_template_version(file_path)
        _tp = TEMPLATE_VIDEO_PATH if version == VIDEO_TEMPLATE_VERSION else None
        html = _render_html(topic, chapters, segments or [], template_path=_tp)
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(file_path, html)
        _remember_template_version(file_path, version)

    record.status = status
    record.chapters_json = json.dumps(chapters, ensure_ascii=False)
    record.total_duration_ms = sum(c.get("total_duration_ms", 0) for c in chapters)
    await record.save()

    await _notify_sse(record.id, {
        "status": status,
        "chapters": len(chapters),
        "file_url": _versioned_presentation_url(record.file_url),
    })


# ═══════════════════════════════════════════════
#  异步工具
# ═══════════════════════════════════════════════

def asyncio_create_task(coro):
    """安全创建后台任务"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(coro, loop=loop)
        return task
    except RuntimeError:
        logger.debug("[视频] 当前线程没有可用事件循环，跳过预热任务调度", exc_info=True)


# ═══════════════════════════════════════════════
#  资源获取
# ═══════════════════════════════════════════════

async def _generate_fresh_video_resources(
    topic: str,
    user_id: int,
    record_id: int,
    answers: dict | None = None,
    chat_group_id: int = 0,
    on_resource_complete=None,
    on_ppt_section_complete=None,
) -> tuple:
    from backend.src.ai_core.resource_graph import resource_graph
    from backend.src.service.resource.generation_context import make_generation_state

    initial_state = await make_generation_state(
        topic,
        user_id,
        ["document", "ppt"],
        chat_group_id=chat_group_id,
        answers=answers,
        skip_review=True,
        ppt_prompt_key="ppt",
        llm_priority="high",
    )
    actual_topic = initial_state.get("topic") or topic
    generated: dict[str, object] = {}
    notified_resources: set[str] = set()

    async def _call_handler(handler, *args):
        if not handler:
            return
        result = handler(*args)
        if asyncio.iscoroutine(result):
            await result

    async for mode, chunk in resource_graph.astream(initial_state, stream_mode=["values", "custom"]):
        if not isinstance(chunk, dict):
            continue

        if mode == "custom":
            event_type = chunk.get("type")
            if event_type == "ppt_section_complete":
                content = str(chunk.get("content") or "").strip()
                if content:
                    await _call_handler(
                        on_ppt_section_complete,
                        int(chunk.get("section_idx") or 0),
                        str(chunk.get("section_title") or ""),
                        int(chunk.get("section_total") or 0),
                        content,
                    )
                continue

            if event_type == "resource_complete":
                rt = str(chunk.get("resource_type") or chunk.get("file_type") or "").strip()
                content = chunk.get("content")
                if rt and content is not None:
                    generated[rt] = content
                    if rt not in notified_resources:
                        resource_id = record_id * 10 + (1 if rt == "document" else 2 if rt == "ppt" else 9)
                        resource = SimpleNamespace(
                            id=resource_id,
                            topic=actual_topic,
                            resource_type=rt,
                            content=str(content or ""),
                        )
                        await _call_handler(on_resource_complete, rt, resource)
                        notified_resources.add(rt)
                continue

        elif mode == "values":
            resources = chunk.get("generated_resources", {}) or {}
            if isinstance(resources, dict):
                generated.update({rt: content for rt, content in resources.items() if content is not None})

    doc_content = str(generated.get("document") or "").strip()
    ppt_content = str(generated.get("ppt") or "").strip()

    doc = SimpleNamespace(
        id=record_id * 10 + 1,
        topic=actual_topic,
        resource_type="document",
        content=doc_content,
    ) if doc_content else None
    ppt_data = SimpleNamespace(
        id=record_id * 10 + 2,
        topic=actual_topic,
        resource_type="ppt",
        content=ppt_content,
    ) if ppt_content else None

    logger.info(
        "[视频] fresh resources generated record=%d doc=%s ppt=%s",
        record_id,
        bool(doc),
        bool(ppt_data),
    )
    return doc, None, ppt_data


async def _fetch_resources(topic: str, user_id: int) -> tuple:
    records = await GeneratedResource.filter(
        user_id=user_id, topic=topic,
    ).order_by("-created_at").all()

    doc = mindmap_data = ppt_data = None
    for r in records:
        if r.resource_type == "document" and not doc:
            doc = r
        elif r.resource_type == "mindmap" and not mindmap_data:
            mindmap_data = r
        elif r.resource_type == "ppt" and not ppt_data:
            ppt_data = r
    return doc, mindmap_data, ppt_data


# ═══════════════════════════════════════════════
#  骨架构建 — 只解析内容，不生成音频
# ═══════════════════════════════════════════════

async def _build_portrait_intro(topic: str, user) -> dict | None:
    """根据用户画像生成个性化学习引入（第一章节）"""
    from backend.src.models.portraitmodel import User_picture

    picture = None
    try:
        picture = await user.picture
    except Exception:
        logger.warning("获取用户画像失败 user_id=%s", user.id)
    if not picture:
        picture = await User_picture.filter(id=getattr(user, 'picture_id', None) or 0).first()

    portrait_parts = []
    if user.major:
        portrait_parts.append(f"专业：{user.major}")
    if user.grade:
        portrait_parts.append(f"年级：{user.grade}")
    if picture:
        if picture.learning_goal:
            portrait_parts.append(f"学习目标：{picture.learning_goal}")
        if picture.cognition:
            portrait_parts.append(f"认知风格：{picture.cognition}")
        if picture.personality_tags:
            try:
                tags = json.loads(picture.personality_tags)
                portrait_parts.append(f"性格：{'、'.join(tags)}")
            except (json.JSONDecodeError, TypeError):
                logger.warning("解析性格标签失败 user_id=%s", user.id)
        if picture.profile_summary:
            portrait_parts.append(f"学习画像：{picture.profile_summary}")
    portrait_text = "；".join(portrait_parts) if portrait_parts else "暂无画像数据"

    prompt = fill_prompt(
        load_prompt("presentation/portrait_intro"),
        topic=topic,
        portrait_text=portrait_text,
    )

    try:
        resp = await llm.ainvoke(prompt, priority="high", user_id=getattr(user, "id", 0) or 0, pool="leader")
        intro_text = resp.content.strip()
        if intro_text.startswith("```"):
            intro_text = re.sub(r"^```\w*\n?", "", intro_text)
            intro_text = re.sub(r"\n?```$", "", intro_text)
    except Exception:
        logger.exception("生成画像引入失败")
        return None

    if not intro_text or len(intro_text) < 10:
        return None

    from backend.src.utils.tts_utils import parse_text_sections
    sections = parse_text_sections(intro_text)
    slides = []
    for sec in sections:
        slides.append({
            "title": sec.get("title") or topic,
            "content_html": _md_to_html(sec.get("text", "")),
            "audio_url": None,
            "duration_ms": sec.get("duration_ms", 5000),
            "word_timestamps": [],
        })

    total_dur = sum(s.get("duration_ms", 0) for s in slides)
    return {
        "type": "intro",
        "title": "学习引入",
        "slides": slides,
        "total_duration_ms": total_dur,
        "is_audio_ready": False,
        "_raw_text": intro_text,
    }


def _build_intro_skeleton(record, max_slides: int = 8) -> dict:
    from backend.src.utils.tts_utils import parse_text_sections
    content = record.content or ""

    # 先按标题切分，有标题时保留原始 markdown 给 HTML 渲染
    raw_parts = re.split(r"\n(?=## )", content.strip())
    if len(raw_parts) <= 1:
        raw_parts = re.split(r"\n(?=# )", content.strip())

    slides = []
    if len(raw_parts) > 1:
        # 有标题 → 和 parse_text_sections 一致，保留原始 markdown
        for part in raw_parts[:max_slides]:
            part = part.strip()
            if not part:
                continue
            lines = part.split("\n")
            title = lines[0].lstrip("#").strip()
            slides.append({
                "title": title,
                "content_html": _md_to_html(part),
                "audio_url": None,
                "duration_ms": int(len(part) / 4 * 1000),
                "word_timestamps": [],
            })
    else:
        # 无标题 → 用 parse_text_sections 确保 slide 数 = TTS 分段数
        sections = parse_text_sections(content)
        for sec in sections[:max_slides]:
            slides.append({
                "title": sec.get("title") or record.topic,
                "content_html": _md_to_html(sec.get("text", "")),
                "audio_url": None,
                "duration_ms": sec.get("duration_ms", 5000),
                "word_timestamps": [],
            })

    total_dur = sum(s.get("duration_ms", 0) for s in slides)
    return {
        "type": "intro",
        "title": record.topic,
        "slides": slides,
        "total_duration_ms": total_dur,
        "is_audio_ready": False,
    }


def _build_mindmap_skeleton(record, svg: str) -> dict:
    return {
        "type": "mindmap",
        "title": record.topic,
        "content_html": svg,
        "audio_segments": [],
        "total_duration_ms": 30000,
        "is_audio_ready": False,
    }


def _build_ppt_skeleton(record, plain: bool = False) -> dict:
    from backend.src.utils.tts_utils import parse_slides
    content = record.content or ""
    slides_meta = parse_slides(content)

    slides = []
    for meta in slides_meta:
        bullets = meta.get("bullets") or []
        text = meta.get("text", "")
        estimated_dur = len(text) / 4 * 1000 if text else 5000
        slide = {
            "title": meta.get("title", ""),
            "text": meta.get("text", ""),
            "bullets": bullets,
            "blocks": meta.get("blocks", []),
            "layout": meta.get("layout", "content_cards"),
            "theme": meta.get("theme", "academic_blue"),
            "palette": meta.get("palette", []),
            "visual": meta.get("visual", {}),
            "notes": meta.get("notes", ""),
            "audio_url": None,
            "duration_ms": int(estimated_dur),
            "word_timestamps": [],
        }
        if plain:
            slide["video_safe"] = True
        slides.append(slide)

    total_dur = sum(s.get("duration_ms", 0) for s in slides)
    return {
        "type": "ppt",
        "title": record.topic,
        "slides": slides,
        "total_duration_ms": total_dur,
        "is_audio_ready": False,
    }


# ═══════════════════════════════════════════════
#  音频补丁 — 把 narration 结果注入骨架
# ═══════════════════════════════════════════════

def _patch_intro_audio(ch: dict, narration: dict):
    segments = (narration or {}).get("sections", [])
    # 按 index 建立索引，避免 TTS 部分段失败导致位置错位
    seg_by_idx = {seg["index"]: seg for seg in segments if seg and "index" in seg}
    slides = ch.get("slides", [])
    for i, slide in enumerate(slides):
        seg = seg_by_idx.get(i)
        if seg:
            slide["audio_url"] = seg.get("audio_url")
            slide["duration_ms"] = seg.get("duration_ms", slide["duration_ms"])
            slide["word_timestamps"] = seg.get("word_timestamps", [])
    # total_duration_ms 只统计有音频的幻灯片（含无音频幻灯片的骨架估值）
    total = sum(s.get("duration_ms", 0) for s in slides)
    ch["total_duration_ms"] = total
    ch["audio_slide_count"] = sum(1 for s in slides if s.get("audio_url"))


def _patch_mindmap_audio(ch: dict, narration: dict):
    segments = (narration or {}).get("sections", [])
    ch["audio_segments"] = segments
    ch["total_duration_ms"] = sum(s.get("duration_ms", 0) for s in segments)


def _patch_ppt_audio(ch: dict, narration: dict):
    segments = (narration or {}).get("sections", [])
    seg_by_idx = {seg["index"]: seg for seg in segments if seg and "index" in seg}
    slides = ch.get("slides", [])
    for i, slide in enumerate(slides):
        seg = seg_by_idx.get(i)
        if seg:
            slide["audio_url"] = seg.get("audio_url")
            slide["duration_ms"] = seg.get("duration_ms", slide["duration_ms"])
            slide["word_timestamps"] = seg.get("word_timestamps", [])
    total = sum(s.get("duration_ms", 0) for s in slides)
    ch["total_duration_ms"] = total
    ch["audio_slide_count"] = sum(1 for s in slides if s.get("audio_url"))


# ═══════════════════════════════════════════════
#  Markdown → HTML
# ═══════════════════════════════════════════════

def _md_to_html(md: str) -> str:
    lines = md.strip().split("\n")
    parts = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue
        if stripped.startswith("# "):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h1>{_escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h2>{_escape(stripped[3:])}</h2>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            text = _inline_bold(_escape(stripped[2:]))
            parts.append(f"<li>{text}</li>")
        elif stripped.startswith("> "):
            parts.append(f"<blockquote>{_escape(stripped[2:])}</blockquote>")
        else:
            if in_list:
                parts.append("</ul>")
                in_list = False
            text = _inline_bold(_escape(stripped))
            parts.append(f"<p>{text}</p>")
    if in_list:
        parts.append("</ul>")
    return "".join(parts)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _inline_bold(text: str) -> str:
    import re
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


# ═══════════════════════════════════════════════
#  思维导图 → SVG
# ═══════════════════════════════════════════════

_NODE_COLORS = ["#4f8cff", "#a78bfa", "#34d399", "#f59e0b", "#ef4444", "#ec4899"]
_NODE_W = 140
_NODE_H = 42
_V_SPACING = 80
_H_SPACING = 24


def _mindmap_to_svg(data: dict) -> str:
    if not data or not data.get("topic"):
        return "<svg></svg>"
    data["_depth"] = 0
    _calc_leaf_x(data, 0)
    total_leaves = _count_leaves(data)
    _center_parents(data)

    pw = max(total_leaves * (_NODE_W + _H_SPACING) + 60, 600)
    ph = max((_max_depth(data) + 1) * (_NODE_H + _V_SPACING) + 40, 400)

    def to_px(node):
        leaves_before = _count_leaves_before(node)
        return leaves_before * (_NODE_W + _H_SPACING) + (_NODE_W / 2) + 30, node.get("_depth", 0) * (_NODE_H + _V_SPACING) + 30

    parts = [f'<svg viewBox="0 0 {pw} {ph}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">']
    parts.append(f'<rect width="{pw}" height="{ph}" fill="transparent"/>')

    def walk(node):
        x, y = to_px(node)
        depth = node.get("_depth", 0)
        color = _NODE_COLORS[depth % len(_NODE_COLORS)]
        children = node.get("children", [])

        if "_parent_x" in node:
            px = node["_parent_x"]
            py = node["_parent_y"]
            mid_y = (y + py) / 2
            parts.append(f'<path d="M{px},{py} L{px},{mid_y} L{x},{mid_y} L{x},{y}" stroke="{color}" stroke-width="1.5" fill="none" opacity="0.4"/>')

        text = _escape(node.get("topic", ""))
        rx = x - _NODE_W / 2
        ry = y - _NODE_H / 2
        parts.append(f'<rect x="{rx}" y="{ry}" width="{_NODE_W}" height="{_NODE_H}" rx="8" fill="{color}" fill-opacity="0.15" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<text x="{x}" y="{y + 5}" text-anchor="middle" fill="#e0e0e0" font-size="13" font-family="Microsoft YaHei, sans-serif">{text}</text>')

        for child in children:
            child["_parent_x"] = x
            child["_parent_y"] = y + _NODE_H / 2
            walk(child)

    data["_depth"] = 0
    walk(data)
    parts.append("</svg>")
    return "".join(parts)


def _calc_leaf_x(node, x_start=0):
    children = node.get("children", [])
    if not children:
        node["_leaf_x"] = x_start
        return x_start + 1
    for child in children:
        x_start = _calc_leaf_x(child, x_start)
    return x_start


def _center_parents(node):
    children = node.get("children", [])
    if not children:
        return
    for child in children:
        child["_depth"] = node.get("_depth", 0) + 1
        _center_parents(child)
    leaves = _count_leaves(node)
    first_x = _first_leaf_x(node)
    node["_leaf_x"] = first_x + (leaves - 1) / 2


def _count_leaves(node) -> int:
    children = node.get("children", [])
    return 1 if not children else sum(_count_leaves(c) for c in children)


def _count_leaves_before(node) -> int:
    return int(node.get("_leaf_x", 0))


def _first_leaf_x(node) -> int:
    children = node.get("children", [])
    if not children:
        return node.get("_leaf_x", 0)
    return _first_leaf_x(children[0])


def _max_depth(node) -> int:
    children = node.get("children", [])
    if not children:
        return node.get("_depth", 0)
    return max(_max_depth(c) for c in children)


# ═══════════════════════════════════════════════
#  HTML 渲染
# ═══════════════════════════════════════════════

def _versioned_presentation_url(url: str | None) -> str:
    if not url:
        return ""
    base = str(url).split("?", 1)[0]
    path = _presentation_file_path(url)
    tag = _detect_template_version(path) if path and path.exists() else PRESENTATION_TEMPLATE_VERSION
    return f"{base}?v={tag}"


def _presentation_file_path(url: str | None) -> Path | None:
    if not url:
        return None
    fname = str(url).split("?", 1)[0].rsplit("/", 1)[-1]
    if not fname:
        return None
    return VIDEOS_DIR / fname


def _presentation_file_matches_template(url: str | None, expected_tag: str | None = None) -> bool:
    path = _presentation_file_path(url)
    if not path or not path.exists():
        return False
    tag = expected_tag or f"template-version:{PRESENTATION_TEMPLATE_VERSION}"
    try:
        return tag in path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        logger.warning("读取 HTML 模板版本失败 url=%s", url)
        return False



def _render_video_slides_html(sections: list[dict]) -> str:
    import re as _re

    def clean(value) -> str:
        text = str(value or "")
        text = _re.sub(r"<!--[\s\S]*?-->", " ", text)
        text = _re.sub(r"</?[^>\n]+>", " ", text)
        text = _re.sub(r"^\s*(layout|theme|visual)\s*:\s*.*$", " ", text, flags=_re.IGNORECASE | _re.MULTILINE)
        text = _re.sub(r"\s+", " ", text).strip()
        return text

    def safe(value) -> str:
        return _escape(clean(value))

    def slide_blocks(sl: dict) -> list[str]:
        texts: list[str] = []
        for block in sl.get("blocks") or []:
            raw = block.get("text") if isinstance(block, dict) else block
            text = clean(raw)
            if text:
                texts.append(text)
        if not texts:
            texts = [clean(item) for item in (sl.get("bullets") or [])]
            texts = [item for item in texts if item]
        if not texts:
            raw_text = clean(sl.get("text", ""))
            texts = [item.strip() for item in _re.split(r"[。；;]\s*|\n+", raw_text) if item.strip()]
        return texts[:8]

    def visual_html(sl: dict, block_id: str) -> str:
        visual = sl.get("visual") or {}
        kind = clean(visual.get("type") or "diagram")
        caption = safe(visual.get("caption") or visual.get("query") or sl.get("title"))
        return (
            f'<div class="video-visual video-visual--{_escape(kind)}" data-narration-block="{block_id}">'
            '<div class="visual-ring"></div><div class="visual-line visual-line--a"></div>'
            '<div class="visual-line visual-line--b"></div><div class="visual-dot visual-dot--a"></div>'
            '<div class="visual-dot visual-dot--b"></div><div class="visual-dot visual-dot--c"></div>'
            f'<p>{caption}</p></div>'
        )

    def item_html(tag: str, text: str, block_id: str, index: int | None = None) -> str:
        badge = f"<b>{index}</b>" if index is not None else ""
        return f'<{tag} data-narration-block="{block_id}">{badge}<span>{_escape(text)}</span></{tag}>'

    def stage_chrome(slide_index: int) -> str:
        progress = min(94, max(12, 18 + slide_index * 9))
        wave = "".join(f'<i style="--i:{i}"></i>' for i in range(1, 19))
        return (
            '<div class="video-slide__meta">'
            f'<span>AI 课程视频</span><b>第 {slide_index + 1} 页</b>'
            '</div>'
            f'<div class="video-slide__wave">{wave}</div>'
            f'<div class="video-slide__progress"><i style="--progress:{progress}%"></i></div>'
        )

    def render_ppt_slide(sl: dict, slide_index: int) -> str:
        title = safe(sl.get("title") or f"Slide {slide_index + 1}")
        layout = clean(sl.get("layout") or "content_cards")
        items = slide_blocks(sl)
        title_block = f"ppt-{slide_index}-title"

        if layout == "process_steps":
            cards = "".join(item_html("article", item, f"ppt-{slide_index}-block-{i}", i + 1) for i, item in enumerate(items[:4]))
            body = f'<div class="video-process">{cards}</div>'
        elif layout == "comparison":
            left = "".join(item_html("li", item, f"ppt-{slide_index}-left-{i}") for i, item in enumerate(items[::2][:3]))
            right = "".join(item_html("li", item, f"ppt-{slide_index}-right-{i}") for i, item in enumerate(items[1::2][:3]))
            body = (
                '<div class="video-compare">'
                f'<section><h3>A</h3><ul>{left}</ul></section>'
                f'<section><h3>B</h3><ul>{right}</ul></section>'
                '</div>'
            )
        elif layout == "formula_focus":
            formula = next((item for item in items if "=" in item or "$" in item or "\\" in item), items[0] if items else "")
            rest = [item for item in items if item != formula][:4]
            points = "".join(item_html("li", item, f"ppt-{slide_index}-formula-{i}") for i, item in enumerate(rest))
            body = (
                f'<div class="video-formula" data-narration-block="ppt-{slide_index}-formula-main">{_escape(formula)}</div>'
                f'<ul class="video-points">{points}</ul>'
            )
        elif layout == "concept_visual":
            bullets = "".join(item_html("li", item, f"ppt-{slide_index}-point-{i}") for i, item in enumerate(items[:5]))
            body = (
                '<div class="video-split">'
                f'<ul class="video-points">{bullets}</ul>'
                f'{visual_html(sl, f"ppt-{slide_index}-visual")}'
                '</div>'
            )
        else:
            cards = "".join(item_html("article", item, f"ppt-{slide_index}-card-{i}", i + 1) for i, item in enumerate(items[:6]))
            body = f'<div class="video-card-grid">{cards}</div>'

        return f'<div class="slide video-slide video-slide--{_escape(layout)}">{stage_chrome(slide_index)}<h2 data-narration-block="{title_block}">{title}</h2>{body}</div>'

    parts: list[str] = []
    ppt_index = 0
    for sec in sections:
        sec_type = sec.get("type", "")
        if sec_type == "ppt":
            for sl in sec.get("slides", []):
                parts.append(render_ppt_slide(sl, ppt_index))
                ppt_index += 1
        elif sec_type in ("intro", "reading"):
            for index, sl in enumerate(sec.get("slides", [])):
                title = safe(sl.get("title") or sec.get("title"))
                text = clean(sl.get("intro_text") or sl.get("text") or sl.get("content_html"))
                paras = "".join(f'<p data-narration-block="intro-{index}-{i}">{_escape(p.strip())}</p>' for i, p in enumerate(_re.split(r"[。；;]\s*", text)) if p.strip())
                parts.append(f'<div class="slide video-slide video-slide--intro">{stage_chrome(len(parts))}<h2 data-narration-block="intro-{index}-title">{title}</h2><div class="intro-content">{paras}</div></div>')
        elif sec_type == "mindmap":
            html = sec.get("content_html", "")
            parts.append(f'<div class="slide video-slide video-slide--mindmap">{stage_chrome(len(parts))}<h2 data-narration-block="mindmap-title">思维导图</h2><div id="mindmap-container" data-narration-block="mindmap-body">{html}</div></div>')
    return "\n".join(parts)


def _render_html(topic: str, sections: list[dict], segments: list[dict] | None = None, template_path: Path | None = None) -> str:
    template = _read_template(template_path or TEMPLATE_PATH)
    sections_json = json.dumps(sections, ensure_ascii=False, indent=2)
    topic_json = json.dumps(topic, ensure_ascii=False)
    segments_json = json.dumps(segments or [], ensure_ascii=False)

    is_video = template_path == TEMPLATE_VIDEO_PATH
    slides_html = _render_video_slides_html(sections) if is_video else ""
    html = template.replace("{{SECTIONS}}", sections_json)
    html = html.replace("{{TITLE_JSON}}", topic_json)  # 必须在 {{TITLE}} 之前替换，否则 {{TITLE}} 会匹配到 {{TITLE_JSON}} 的前缀
    html = html.replace("{{TITLE}}", _escape(topic))
    html = html.replace("{{AUDIO_SEGMENTS}}", segments_json)
    html = html.replace("{{SLIDES_HTML}}", slides_html)
    version_tag = f"<!-- template-version:{VIDEO_TEMPLATE_VERSION} -->" if is_video else f"<!-- template-version:{PRESENTATION_TEMPLATE_VERSION} -->"
    return f"{version_tag}\n{html}"


def _safe_filename(topic: str) -> str:
    return "".join(c for c in topic if c.isalnum() or c in " _-")[:30]
