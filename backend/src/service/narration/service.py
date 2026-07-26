"""语音旁白服务 — 对文字类资源调用 EdgeTTS 逐段生成旁白 (CRUD)"""

import asyncio
import hashlib
import json
import logging
import os
import time as _time
from pathlib import Path
import shutil

from backend.src.utils.tts_utils import parse_by_type, generate_audio_with_timestamps, NARRATABLE_TYPES
from backend.src.utils.exceptions import ServiceError
from backend.src.utils.constants import AUDIO_DIR

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# EdgeTTS 并发上限（微软免费服务，减少并发避免带宽挤占和超时）
TTS_GLOBAL_CONCURRENCY = max(1, _int_env("TTS_GLOBAL_CONCURRENCY", 8))
TTS_PER_USER_CONCURRENCY = max(1, _int_env("TTS_PER_USER_CONCURRENCY", 8))
_TTS_SEMAPHORE = asyncio.Semaphore(TTS_GLOBAL_CONCURRENCY)
_TTS_USER_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _get_tts_user_semaphore(user_id: int | str | None) -> asyncio.Semaphore:
    key = str(user_id or "anonymous")
    sem = _TTS_USER_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(TTS_PER_USER_CONCURRENCY)
        _TTS_USER_SEMAPHORES[key] = sem
    return sem

_tts_cache_key = lambda text, voice: hashlib.md5(f"{text}_{voice}".encode()).hexdigest()[:12]

# 全局 TTS 计数器（跨所有调用点共享）
_tts_done_count = 0
_tts_lock = asyncio.Lock()


async def narrate_content(content: str, resource_type: str, voice: str, resource_id: int) -> dict:
    """从原始内容字符串直接生成旁白（不查 DB，不写 Narration 表），用于裁剪后的内容

    Returns:
        {"sections": [...]}  与 narrate_resource 返回的 sections 格式一致
    """
    sections = parse_by_type(content, resource_type or "document")
    if not sections:
        return {"sections": []}

    base_dir = AUDIO_DIR / str(resource_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    results = await _generate_sections_audio(sections, voice, base_dir, resource_id)
    return {"sections": results}


async def narrate_resource(resource_id: int, voice: str = "zh-CN-XiaoxiaoNeural", force_regenerate: bool = False):
    """对某个文字类资源逐段生成旁白，已有则直接返回，无则生成写入 DB

    Args:
        resource_id: 资源 ID
        voice: EdgeTTS 语音名称
        force_regenerate: 是否强制重新生成（删除旧音频 + 旧记录）

    Returns:
        {"sections": [...], "narration_id": int, "resource_id": int, "voice": str, "cached": bool}
    """
    from backend.src.models.resource_model import GeneratedResource
    from backend.src.models.narration_model import Narration

    resource = await GeneratedResource.filter(id=resource_id).first()
    if not resource:
        raise ServiceError("资源不存在")
    if resource.resource_type not in NARRATABLE_TYPES:
        raise ServiceError(f"资源类型 {resource.resource_type} 不支持旁白，仅支持：{', '.join(sorted(NARRATABLE_TYPES))}")

    # 强制重生成 → 删旧记录和音频文件
    if force_regenerate:
        existing = await Narration.filter(resource_id=resource_id, voice=voice).first()
        if existing:
            audio_dir = AUDIO_DIR / str(resource_id)
            if audio_dir.exists():
                shutil.rmtree(audio_dir)
            await existing.delete()

    # 已有旁白 → 直接返回
    existing = await Narration.filter(resource_id=resource_id, voice=voice).first()
    if existing:
        return {
            "sections": existing.slides_json,
            "narration_id": existing.id,
            "resource_id": resource_id,
            "voice": voice,
            "cached": True,
        }

    sections = parse_by_type(resource.content or "", resource.resource_type or "document")
    if not sections:
        raise ServiceError("无法解析内容，资源可能为空")

    base_dir = AUDIO_DIR / str(resource_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    results = await _generate_sections_audio(sections, voice, base_dir, resource_id, user_id=resource.user_id)

    # 入库
    record = await Narration.create(
        resource=resource,
        voice=voice,
        slides_json=results,
    )

    return {
        "narration_id": record.id,
        "sections": results,
        "resource_id": resource_id,
        "voice": voice,
        "cached": False,
    }


async def _generate_tts(text: str, voice: str, output_path: str, user_id: int | str | None = None) -> list[dict] | None:
    """核心 TTS 生成：限流 + 重试 + 超时处理 + 保存 JSON 时间戳。
    长文本自动按句子切分，各分段并行生成后按序拼接 MP3。"""
    json_path = output_path.rsplit(".", 1)[0] + ".json"

    chunks = _split_long_text(text)
    if len(chunks) == 1:
        return await _generate_tts_one(text, voice, output_path, json_path, user_id=user_id)

    # 长文本：并行生成各分段 → 按序拼接 MP3 + 时间戳
    import tempfile

    async def _one_chunk(i: int, chunk: str):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            part_path = tmp.name
        part_json = part_path.rsplit(".", 1)[0] + ".json"
        timestamps = await _generate_tts_one(chunk, voice, part_path, part_json, user_id=user_id)
        if timestamps is None:
            for f in (part_path, part_json):
                try:
                    os.remove(f)
                except OSError:
                    logger.debug("[TTS] 清理临时分段文件失败 path=%s", f, exc_info=True)
            return None
        return (i, part_path, timestamps)

    results = await asyncio.gather(*[_one_chunk(i, c) for i, c in enumerate(chunks)])

    # 检查失败 + 按序排列
    part_results: list[tuple[int, str, list[dict]]] = []
    for r in results:
        if r is None:
            for _, p, _ in part_results:
                for f in (p, p.rsplit(".", 1)[0] + ".json"):
                    try:
                        os.remove(f)
                    except OSError:
                        logger.debug("[TTS] 清理已生成分段文件失败 path=%s", f, exc_info=True)
            return None
        part_results.append(r)
    part_results.sort(key=lambda x: x[0])

    # 按序拼接 MP3 + 合并时间戳（同步 IO 放入线程避免阻塞事件循环）
    try:
        all_timestamps = await asyncio.to_thread(
            _concat_mp3_and_timestamps, output_path, json_path, part_results
        )
    except IOError:
        logger.exception("MP3 拼接失败 output=%s", output_path)
        return None
    finally:
        for _, part_path, _ in part_results:
            for f in (part_path, part_path.rsplit(".", 1)[0] + ".json"):
                try:
                    os.remove(f)
                except OSError:
                    logger.debug("[TTS] 清理临时拼接文件失败 path=%s", f, exc_info=True)

    return all_timestamps


async def _generate_tts_one(text: str, voice: str, output_path: str, json_path: str, user_id: int | str | None = None) -> list[dict] | None:
    """单段 TTS 生成（不切分），限流 + 重试 + 超时处理"""
    user_sem = _get_tts_user_semaphore(user_id)
    async with user_sem, _TTS_SEMAPHORE:
        t0 = _time.perf_counter()
        last_err = None
        for attempt in range(3):
            try:
                _, word_timestamps = await generate_audio_with_timestamps(text, output_path, voice)
                break
            except asyncio.TimeoutError as e:
                last_err = e
                logger.warning("[TTS] 超时 text_len=%d attempt=%d", len(text), attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
                continue
            except (ConnectionResetError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except Exception:
                logger.exception("[TTS] 生成失败 text_len=%d", len(text))
                return None
        else:
            logger.error("[TTS] 重试耗尽 text_len=%d err=%s", len(text), last_err)
            return None

        cost = _time.perf_counter() - t0

    try:
        await asyncio.to_thread(_dump_json, json_path, word_timestamps)
    except IOError:
        logger.exception("[TTS] 时间戳 JSON 写入失败 path=%s", json_path)

    async with _tts_lock:
        global _tts_done_count
        _tts_done_count += 1
        done = _tts_done_count

    logger.info("[TTS] #%d user=%s len=%d cost=%.1fs", done, user_id or "-", len(text), cost)
    return word_timestamps


_MAX_TTS_CHARS = 800  # 单次 TTS 文本上限，切成小段降低超时风险

def _concat_mp3_and_timestamps(output_path: str, json_path: str, part_results: list) -> list[dict]:
    """同步拼接 MP3 片段并合并时间戳（在线程中执行，避免阻塞事件循环）"""
    all_timestamps: list[dict] = []
    offset_ms = 0
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as out:
        for _, part_path, timestamps in part_results:
            with open(part_path, "rb") as inp:
                out.write(inp.read())
            for ts in timestamps:
                ts["offset_ms"] = ts.get("offset_ms", 0) + offset_ms
            all_timestamps.extend(timestamps)
            if timestamps:
                last = timestamps[-1]
                offset_ms += last.get("offset_ms", 0) + last.get("duration_ms", 0)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_timestamps, f, ensure_ascii=False)
    return all_timestamps


def _dump_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_json(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _split_long_text(text: str, max_chars: int = _MAX_TTS_CHARS) -> list[str]:
    """将长文本按句子边界拆分为适合 TTS 的短片段"""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        split_at = max_chars
        for sep in ("。", "！", "？", "；", "\n", "，", "、", " "):
            pos = remaining.rfind(sep, 0, max_chars)
            if pos > max_chars // 2:
                split_at = pos + 1
                break
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks



async def _generate_sections_audio(sections: list[dict], voice: str, base_dir: Path, resource_id: int, user_id: int | str | None = None) -> list[dict]:
    """并行生成所有段落的 TTS 音频，复用内容哈希缓存"""
    total = len(sections)
    cache_hits = 0
    _hits_lock = asyncio.Lock()

    async def _tts_one(i: int, section: dict) -> dict | None:
        nonlocal cache_hits
        text = section.get("text", "")
        if not text:
            return None

        cache_key = _tts_cache_key(text, voice)
        output_path = str(base_dir / f"{cache_key}.mp3")
        json_path = str(base_dir / f"{cache_key}.json")
        audio_url = f"/static/audio/{resource_id}/{cache_key}.mp3"

        if os.path.exists(output_path) and os.path.exists(json_path):
            try:
                word_timestamps = await asyncio.to_thread(_load_json, json_path)
                real_dur = _real_duration_from_timestamps(word_timestamps, section["duration_ms"])
                async with _hits_lock:
                    cache_hits += 1
                return {
                    "index": i,
                    "title": section["title"],
                    "text": text,
                    "audio_url": audio_url,
                    "duration_ms": real_dur,
                    "word_timestamps": word_timestamps,
                }
            except (json.JSONDecodeError, IOError):
                logger.warning(
                    "[Narration] TTS 缓存读取失败，重新生成 resource=%s section=%s json=%s",
                    resource_id, i, json_path,
                    exc_info=True,
                )

        word_timestamps = await _generate_tts(text, voice, output_path, user_id=user_id)
        if word_timestamps is None:
            return None

        real_dur = _real_duration_from_timestamps(word_timestamps, section["duration_ms"])
        return {
            "index": i,
            "title": section["title"],
            "text": text,
            "audio_url": audio_url,
            "duration_ms": real_dur,
            "word_timestamps": word_timestamps,
        }

    logger.info("[Narration] resource=%d sections=%d 开始", resource_id, total)
    t0 = _time.perf_counter()
    tasks = [_tts_one(i, s) for i, s in enumerate(sections)]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    failures = 0
    for r in raw_results:
        if isinstance(r, dict) and r is not None:
            results.append(r)
        elif isinstance(r, Exception):
            failures += 1
            logger.exception("[Narration] 任务异常: %s", r)
        else:
            failures += 1

    results.sort(key=lambda x: x["index"])
    cost = _time.perf_counter() - t0
    generated = total - cache_hits - failures
    logger.info("[Narration] resource=%d 完成 total=%d 命中=%d 新生成=%d 失败=%d cost=%.1fs",
                resource_id, total, cache_hits, generated, failures, cost)
    return results


def _real_duration_from_timestamps(word_timestamps: list[dict], fallback_ms: int) -> int:
    """从词级时间戳计算真实音频时长（最后一个词的 offset + duration），无时间戳时回退到估算值"""
    if word_timestamps:
        last = word_timestamps[-1]
        real = last.get("offset_ms", 0) + last.get("duration_ms", 0)
        if real > 0:
            return real
    return fallback_ms


async def list_narrations(user_id: int) -> list[dict]:
    """列出某个用户的所有旁白记录"""
    from backend.src.models.narration_model import Narration

    records = await Narration.filter(resource__user_id=user_id).prefetch_related("resource").order_by("-created_at").all()
    return [
        {
            "narration_id": r.id,
            "resource_id": r.resource_id,
            "resource_topic": r.resource.topic if r.resource else "",
            "resource_type": r.resource.resource_type if r.resource else "",
            "voice": r.voice,
            "sections_count": len(r.slides_json) if r.slides_json else 0,
            "total_duration_ms": sum(s.get("duration_ms", 0) for s in (r.slides_json or [])),
            "created_at": str(r.created_at),
        }
        for r in records
    ]


async def get_narration(narration_id: int, user_id: int) -> dict | None:
    """获取单个旁白详情"""
    from backend.src.models.narration_model import Narration

    record = await Narration.filter(id=narration_id, resource__user_id=user_id).prefetch_related("resource").first()
    if not record:
        return None
    return {
        "narration_id": record.id,
        "resource_id": record.resource_id,
        "resource_topic": record.resource.topic if record.resource else "",
        "resource_type": record.resource.resource_type if record.resource else "",
        "voice": record.voice,
        "sections": record.slides_json,
        "created_at": str(record.created_at),
    }


async def delete_narration(narration_id: int, user_id: int) -> bool:
    """删除旁白记录及对应音频文件夹"""
    from backend.src.models.narration_model import Narration

    record = await Narration.filter(id=narration_id, resource__user_id=user_id).first()
    if not record:
        return False

    resource_id = record.resource_id

    # 检查该 resource 是否有其他 voice 的旁白，没有才删文件夹
    others = await Narration.filter(resource_id=resource_id).exclude(id=narration_id).count()
    if others == 0:
        audio_dir = AUDIO_DIR / str(resource_id)
        if audio_dir.exists():
            shutil.rmtree(audio_dir)

    await record.delete()
    return True
