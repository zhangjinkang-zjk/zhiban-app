"""
记忆检索与上下文构建 — 读路径，无 LLM 调用。

把工作摘要 + 长期 KV + 跨组情景 + 原文语义检索拼成一个 ≤ 上限字数的
字符串，经 {memory_context} 占位符注入 system prompt。

安全边界：
  - 所有 SQL 硬过滤 user_id
  - 跨组注入受相似度阈值 + 同组加权控制，subjects 只做排序不做绕过
"""

from __future__ import annotations

import json
import logging
import math
import os
import time as _time
from datetime import datetime

import numpy as np
from tortoise.expressions import Q

from backend.src.models.memory_episode_model import MemoryEpisode
from backend.src.models.memory_kv_model import MemoryKV
from backend.src.models.memory_message_model import MemoryMessage
from backend.src.models.memory_summary_model import MemorySummary
from backend.src.service.memory.embedding import encode

logger = logging.getLogger(__name__)

MEMORY_CONTEXT_MAX_CHARS = int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "900"))
MEMORY_SIM_THRESHOLD = float(os.getenv("MEMORY_SIM_THRESHOLD", "0.30"))
SAME_GROUP_BOOST = float(os.getenv("MEMORY_SAME_GROUP_BOOST", "1.35"))
CONTEXT_TTL_SECONDS = int(os.getenv("MEMORY_CONTEXT_TTL_SECONDS", "5"))
KV_TOP_K = int(os.getenv("MEMORY_KV_TOP_K", "12"))
EPISODE_TOP_K = int(os.getenv("MEMORY_EPISODE_TOP_K", "3"))
MESSAGE_TOP_K = int(os.getenv("MEMORY_MESSAGE_TOP_K", "3"))


# ---------------------------------------------------------------------------
# 进程内短缓存（读路径热调用，5s TTL）
# ---------------------------------------------------------------------------

_context_cache: dict[tuple[int, int], tuple[float, str]] = {}


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------

def _score(sim: float, same_group: bool, updated_at, importance: float) -> float:
    if sim < MEMORY_SIM_THRESHOLD:
        return float("-inf")
    score = sim
    if same_group:
        score *= SAME_GROUP_BOOST
    if updated_at:
        # MySQL 返回的 datetime 可能带时区(aware)，datetime.now() 是 naive，
        # 直接相减会抛 TypeError —— 统一用 updated_at 的时区构造 now
        now = datetime.now(updated_at.tzinfo) if updated_at.tzinfo else datetime.now()
        age_days = max((now - updated_at).days, 0)
        score *= 0.5 + 0.5 * math.exp(-age_days / 30.0)
    score *= importance
    return score


def _cosine(qvec, emb_str: str) -> float:
    try:
        vec = np.array(json.loads(emb_str), dtype=np.float32)
        if vec.shape != qvec.shape:
            return 0.0
        return float(np.dot(qvec, vec))
    except Exception:
        # 向量数据损坏（JSON 解析失败）不是正常数据，要留痕不能静默
        logger.warning("记忆向量解析失败，忽略该条: %s", str(emb_str)[:60], exc_info=True)
        return 0.0


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

async def retrieve_episodes(user_id: int, chat_group_id: int, query: str, top_k: int = EPISODE_TOP_K):
    """跨组情景记忆检索：向量相似度 + 同组加权 + 时间衰减 + importance。"""
    if not query or not query.strip():
        return []
    # 该用户没有任何情景记忆时，直接短路，避免无谓加载 BGE 模型（首次加载很慢）
    if await MemoryEpisode.filter(user_id=user_id, embedding__not_isnull=True).exists() is False:
        return []
    qvec = await encode(query.strip())
    rows = await MemoryEpisode.filter(user_id=user_id, embedding__not_isnull=True).all()
    scored = []
    for r in rows:
        sim = _cosine(qvec, r.embedding or "")
        s = _score(sim, r.chat_group_id == chat_group_id, r.updated_at, r.importance)
        if math.isinf(s):
            continue
        scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, r in scored[:top_k]:
        out.append({
            "chat_group_id": r.chat_group_id,
            "summary": r.summary,
            "score": round(s, 3),
        })
    return out


async def retrieve_messages(user_id: int, chat_group_id: int, query: str, top_k: int = MESSAGE_TOP_K):
    """原文语义检索：命中'你上次说过…'。"""
    if not query or not query.strip():
        return []
    # 没有原文向量时直接短路，避免首次加载 BGE
    if await MemoryMessage.filter(user_id=user_id, embedding__not_isnull=True).exists() is False:
        return []
    qvec = await encode(query.strip())
    rows = await MemoryMessage.filter(user_id=user_id, embedding__not_isnull=True).all()
    scored = []
    for r in rows:
        sim = _cosine(qvec, r.embedding or "")
        s = _score(sim, r.chat_group_id == chat_group_id, r.created_at, r.importance)
        if math.isinf(s):
            continue
        scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, r in scored[:top_k]:
        out.append({"content": r.content, "score": round(s, 3)})
    return out


async def retrieve_kvs(user_id: int, chat_group_id: int, query: str = "", top_k: int = KV_TOP_K):
    """长期 KV：user 级全部 + 本组 group 级；query 命中 subjects 词面加权排前。"""
    rows = await MemoryKV.filter(
        Q(user_id=user_id),
        Q(scope="user") | Q(scope="group", source_group_id=chat_group_id),
    ).order_by("-confidence", "-updated_at").limit(top_k).all()

    out = []
    q = (query or "").lower()
    for r in rows:
        boost = 0
        if q and r.subjects:
            for s in r.subjects:
                if s and s.lower() in q:
                    boost = 1
                    break
        out.append((boost, f"{r.key}：{r.value[:80]}"))
    out.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in out[:top_k]]


# ---------------------------------------------------------------------------
# 上下文构建（注入入口）
# ---------------------------------------------------------------------------

async def build_memory_context(user_id: int, chat_group_id: int, user_query: str = "") -> str:
    """拼装记忆上下文字符串，供 {memory_context} 占位符注入。读路径，无 LLM。"""
    ck = (user_id, chat_group_id)
    now = _time.time()
    cached = _context_cache.get(ck)
    if cached and now - cached[0] < CONTEXT_TTL_SECONDS:
        return cached[1]

    # 总短路：该用户一条记忆都没有（新用户），直接返回空，不碰 BGE / 不查各表
    has_any = any([
        await MemorySummary.filter(user_id=user_id).exists(),
        await MemoryKV.filter(user_id=user_id).exists(),
        await MemoryEpisode.filter(user_id=user_id).exists(),
        await MemoryMessage.filter(user_id=user_id).exists(),
    ])
    if not has_any:
        _context_cache[ck] = (now, "")
        return ""

    parts = []

    # ① 工作记忆滚动摘要（本组早期内容）
    ws = await MemorySummary.filter(
        user_id=user_id, chat_group_id=chat_group_id
    ).first()
    if ws and ws.summary:
        parts.append(f"【本对话早期摘要】{ws.summary[:300]}")

    # ② 长期 KV 事实
    kvs = await retrieve_kvs(user_id, chat_group_id, user_query)
    if kvs:
        parts.append("【长期事实】\n" + "\n".join(f"- {v}" for v in kvs))

    # ③ 跨组情景记忆
    episodes = await retrieve_episodes(user_id, chat_group_id, user_query)
    for ep in episodes:
        tag = "当前会话" if ep["chat_group_id"] == chat_group_id else f"会话{ep['chat_group_id']}"
        parts.append(f"【过往对话片段 · {tag}】{ep['summary'][:180]}")

    # ④ 原文语义检索
    msgs = await retrieve_messages(user_id, chat_group_id, user_query)
    for m in msgs:
        parts.append(f"【过往原话】用户曾问/说：{m['content'][:120]}")

    if not parts:
        ctx = ""
    else:
        header = "记忆信息（系统从历史对话自动抽取，可能与现状冲突，以当前对话为准，仅作参考）："
        ctx = header + "\n\n" + "\n\n".join(parts)
        ctx = ctx[:MEMORY_CONTEXT_MAX_CHARS]

    _context_cache[ck] = (now, ctx)
    return ctx


def invalidate_memory_context(user_id: int, chat_group_id: int = None):
    """用户信息变更或记忆写入后清除缓存。"""
    keys = [k for k in _context_cache if k[0] == user_id and (chat_group_id is None or k[1] == chat_group_id)]
    for k in keys:
        _context_cache.pop(k, None)
