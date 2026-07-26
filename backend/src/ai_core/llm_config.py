"""LLM 配置 + 优先级限流 — 前台请求优先于后台预生成，每用户每池独立并发"""
import asyncio
import hashlib
import json as _json
import logging
import threading
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent.parent.parent / ".env")

api_key = os.getenv("api_key")

_raw_llm = ChatOpenAI(
    model=os.getenv("AI_MODEL", "deepseek-v4-flash"),
    api_key=api_key,
    base_url=os.getenv("AI_BASE_URL", "https://api.deepseek.com"),
    temperature=0.3,
    streaming=True,
    request_timeout=120,  # 单次请求超时 120 秒，避免断连后无限等待
)

# 多模态 LLM（MiMo，用于视觉审查 PPT 截图等）
_vision_llm = ChatOpenAI(
    model=os.getenv("VISION_MODEL", "MiMo-V2.5"),
    api_key=os.getenv("VISION_API_KEY", api_key),
    base_url=os.getenv("VISION_BASE_URL", "https://api.xiaomimimo.com/v1"),
    temperature=0.3,
    streaming=False,
)

# 每用户每池并发上限（峰值按 5 用户同时使用设计，总并发 ≤500）
_PER_USER = {
    "ppt": 20,          # 主力：单个 PPT 请求最多约 19 条章节线，5 用户约 100 路
    "document": 20,     # 文档生成（实际受 ThreadPool 限制，20 已够）
    "path": 8,          # 学习路径，低频
    "leader": 3,        # 大纲规划，单次调用，不需多路
    "reviewer": 50,     # 审核，5 用户 ×50=250，对齐 _review_sem
    "thread": 10,       # 其他同步任务
}
_DEFAULT_PER_USER = 5

_LLM_LOW_PRI_LIMIT = 10       # 有前台请求时，低优先级最多占 10 路

# 每用户每池并发池（异步）
_user_pool_async: dict[tuple[int, str], asyncio.Semaphore] = {}
_user_pool_async_lock = asyncio.Lock()

# 每用户每池并发池（同步 / 线程）
_user_pool_sync: dict[tuple[int, str], threading.BoundedSemaphore] = {}
_user_pool_sync_lock = threading.Lock()

# 优先级限流（全局）：有前台请求时，低优先级全局限流
_async_low = asyncio.Semaphore(_LLM_LOW_PRI_LIMIT)
_async_high_active = 0
_async_high_lock = asyncio.Lock()

_sync_low = threading.BoundedSemaphore(_LLM_LOW_PRI_LIMIT)
_sync_high_active = 0
_sync_high_lock = threading.Lock()


def _get_user_sync_sem(user_id: int, pool: str = "default") -> threading.BoundedSemaphore | None:
    if not user_id:
        return None
    key = (user_id, pool)
    if key in _user_pool_sync:
        return _user_pool_sync[key]
    with _user_pool_sync_lock:
        if key not in _user_pool_sync:
            limit = _PER_USER.get(pool, _DEFAULT_PER_USER)
            _user_pool_sync[key] = threading.BoundedSemaphore(limit)
        return _user_pool_sync[key]


class _PriorityLLM:
    """LLM 代理：每用户每池独立并发 + 全局优先级限流 + 可选的 Redis 响应缓存。"""

    def __init__(self, raw_llm=None, cache_ttl: int = 0):
        self._raw = raw_llm or _raw_llm
        self._cache_ttl = cache_ttl  # 0=不缓存；>0 缓存秒数（如 3600=1h）

    def __getattr__(self, name):
        return getattr(self._raw, name)

    @staticmethod
    def _prompt_to_key(prompt) -> str | None:
        """将 prompt 转为 SHA256 缓存 key（仅纯文本 prompt 可缓存）"""
        try:
            raw = str(prompt)
            if len(raw) < 10:
                return None
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except Exception:
            return None

    async def ainvoke(self, prompt, priority: str = "high", user_id: int = 0, pool: str = "default"):
        # 计算缓存 key（仅在 cache_ttl > 0 时）
        _cache_key_str = self._prompt_to_key(prompt) if self._cache_ttl else None

        # 缓存快速通道：命中直接返回，不走 semaphore
        if _cache_key_str:
            try:
                from backend.src.utils.redis_client import cache_get as _cg, _cache_key as _ck
                _cached = await _cg(_ck("llm", _cache_key_str))
                if _cached is not None and isinstance(_cached, dict) and "content" in _cached:
                    return AIMessage(
                        content=_cached["content"],
                        response_metadata=_cached.get("response_metadata", {}),
                    )
            except Exception:
                logger.debug("LLM cache read failed", exc_info=True)

        user_sem = None
        if user_id:
            key = (user_id, pool)
            if key not in _user_pool_async:
                async with _user_pool_async_lock:
                    if key not in _user_pool_async:
                        limit = _PER_USER.get(pool, _DEFAULT_PER_USER)
                        _user_pool_async[key] = asyncio.Semaphore(limit)
            user_sem = _user_pool_async[key]

        async def _call():
            if user_sem:
                async with user_sem:
                    resp = await self._raw.ainvoke(prompt)
            else:
                resp = await self._raw.ainvoke(prompt)
            # 异步回填缓存（仅非流式结果）
            if _cache_key_str and resp and resp.content and len(str(resp.content).strip()) > 5:
                try:
                    from backend.src.utils.redis_client import cache_set as _cs, _cache_key as _ck2
                    _meta = getattr(resp, "response_metadata", {}) or {}
                    await _cs(_ck2("llm", _cache_key_str), {
                        "content": resp.content,
                        "response_metadata": {k: str(v) for k, v in _meta.items() if isinstance(v, (str, int, float))},
                    }, self._cache_ttl)
                except Exception:
                    logger.debug("LLM cache write failed", exc_info=True)
            return resp

        global _async_high_active
        if priority == "high":
            async with _async_high_lock:
                _async_high_active += 1
            try:
                return await _call()
            finally:
                async with _async_high_lock:
                    _async_high_active -= 1
        else:
            async with _async_high_lock:
                throttled = _async_high_active > 0
            if throttled:
                async with _async_low:
                    return await _call()
            else:
                return await _call()

    def invoke(self, prompt, priority: str = "high", user_id: int = 0, pool: str = "default"):
        user_sem = _get_user_sync_sem(user_id, pool)

        def _call():
            if user_sem:
                with user_sem:
                    return self._raw.invoke(prompt)
            else:
                return self._raw.invoke(prompt)

        global _sync_high_active
        if priority == "high":
            with _sync_high_lock:
                _sync_high_active += 1
            try:
                return _call()
            finally:
                with _sync_high_lock:
                    _sync_high_active -= 1
        else:
            with _sync_high_lock:
                throttled = _sync_high_active > 0
            if throttled:
                with _sync_low:
                    return _call()
            else:
                return _call()


# 环境变量 LLM_CACHE_TTL 控制 LLM 缓存秒数（默认 0=关闭，设置如 300=5分钟）
_DEFAULT_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", "0"))

llm = _PriorityLLM(cache_ttl=_DEFAULT_CACHE_TTL)
llm_vision = _PriorityLLM(_vision_llm, cache_ttl=_DEFAULT_CACHE_TTL)
