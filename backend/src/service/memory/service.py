"""
记忆写入流水线 — 对话结束后台任务，把聊天提炼成多级记忆。

职责：
  1. 工作记忆滚动折叠：早期对话折入 memory_summary.summary（配合 Brain._history 水合）
  2. 长期语义 KV：客观事实写入 memory_kv（时间戳 + 版本冲突）
  3. 情景记忆：会话摘要合并进 memory_episode（带向量，供跨组检索）
  4. 原文索引：重要用户原话写入 memory_message_vector

工程模式对齐 portrait 的 extract_portrait_from_chat：
  - asyncio.create_task 后台 fire-and-forget
  - 每用户冷却 + asyncio.Lock + 水位线（last_processed_id）三重防并发
  - 读路径零 LLM，写路径 priority=low 限流
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from datetime import datetime

from backend.src.ai_core.llm_config import llm
from backend.src.models.chat_history_model import ChatHistory
from backend.src.models.memory_episode_model import MemoryEpisode
from backend.src.models.memory_kv_model import MemoryKV
from backend.src.models.memory_message_model import MemoryMessage
from backend.src.models.memory_summary_model import MemorySummary
from backend.src.service.memory.embedding import encode
from backend.src.utils.json_parser import parse_llm_json
from backend.src.utils.prompt_loader import load_prompt, fill_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 可调参数（环境变量，均有默认值）
# ---------------------------------------------------------------------------

MEMORY_WRITE_INTERVAL = int(os.getenv("MEMORY_WRITE_INTERVAL_SECONDS", "20"))   # 冷却秒数
WORKING_BUFFER_TURNS = int(os.getenv("MEMORY_BUFFER_TURNS", "12"))              # 保留原文的最近轮数
WORKING_SUMMARY_MAX = int(os.getenv("MEMORY_WORKING_SUMMARY_MAX", "500"))       # 滚动摘要最大字数
MAX_KV_PER_USER = int(os.getenv("MEMORY_MAX_KV", "40"))                         # 每用户 user 级 KV 上限
MAX_MESSAGES_PER_USER = int(os.getenv("MEMORY_MAX_MESSAGES", "300"))            # 每用户原文向量上限
AI_TEXT_TRUNCATE = 300                                                          # AI 回答截断字数


# ---------------------------------------------------------------------------
# 进程级并发防护
# ---------------------------------------------------------------------------

_last_write: dict[int, float] = {}           # user_id -> 上次写时间戳
_write_locks: dict[int, asyncio.Lock] = {}   # user_id -> 串行锁


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _write_locks:
        _write_locks[user_id] = asyncio.Lock()
    return _write_locks[user_id]


# ---------------------------------------------------------------------------
# 文本格式化
# ---------------------------------------------------------------------------

def _format_turns(records) -> str:
    """把 ChatHistory 记录列表格式化成 LLM 可读文本。"""
    lines = []
    for r in records:
        req = (r.req or "").strip()
        res = (r.res or "").strip()[:AI_TEXT_TRUNCATE]
        if req:
            lines.append(f"用户：{req}")
        if res:
            lines.append(f"AI：{res}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口：对话结束后台调用
# ---------------------------------------------------------------------------

async def persist_memory_after_chat(user_id: int, chat_group_id: int, agent_id: int | None = None):
    """对话结束后的记忆写入入口。冷却 + 每用户锁 + 调内部写路径。"""
    from backend.src.service.memory.retrieval import invalidate_memory_context

    now = _time.time()
    if now - _last_write.get(user_id, 0) < MEMORY_WRITE_INTERVAL:
        return
    lock = _get_lock(user_id)
    if lock.locked():
        return
    async with lock:
        _last_write[user_id] = now
        try:
            await _persist_inner(user_id, chat_group_id, agent_id)
            # 写入后清读缓存，下一轮对话立即看到新记忆
            invalidate_memory_context(user_id, chat_group_id)
        except Exception:
            logger.exception("memory persist failed user=%s group=%s", user_id, chat_group_id)


async def _persist_inner(user_id: int, chat_group_id: int, agent_id: int | None):
    summary_row, _ = await MemorySummary.get_or_create(
        user_id=user_id, chat_group_id=chat_group_id, agent_id=agent_id,
    )

    # 1) 增量取新消息（水位线之后）
    qs = ChatHistory.filter(user_id=user_id, chat_group_id=chat_group_id)
    if summary_row.last_processed_id:
        qs = qs.filter(id__gt=summary_row.last_processed_id)
    new_records = await qs.order_by("id").all()
    if not new_records:
        return

    recent_text = _format_turns(new_records)

    # 2) 工作记忆折叠：把"最近窗口"之外的早期消息折入滚动摘要
    overflow_section, fold_advance_to = await _collect_fold_records(
        user_id, chat_group_id, summary_row
    )

    # 3) 组装已有事实，交给 LLM 联合抽取
    existing_facts = await _existing_facts_json(user_id)
    template = load_prompt("memory/extract")
    prompt = fill_prompt(
        template,
        existing_facts=existing_facts,
        existing_working_summary=summary_row.summary or "暂无",
        overflow_section=overflow_section or "无",
        recent_messages=recent_text,
    )

    try:
        resp = await llm.ainvoke(prompt, priority="low", user_id=user_id, pool="default")
        data = parse_llm_json(resp.content) or {}
    except Exception:
        logger.exception("memory extract LLM failed user=%s group=%s", user_id, chat_group_id)
        data = {}

    if data and isinstance(data, dict):
        # 4) KV 落库（时间戳 + 版本冲突）
        await _upsert_kvs(user_id, chat_group_id, agent_id, data.get("kv") or [])
        # 5) 情景记忆合并 + 向量
        await _merge_episode(user_id, chat_group_id, agent_id, data, new_records)
        # 6) 重要原文入向量索引
        await _index_messages(user_id, chat_group_id, agent_id, new_records, data)
        # 7) 工作记忆滚动摘要更新
        ws = data.get("working_summary")
        if isinstance(ws, str) and ws.strip():
            summary_row.summary = ws.strip()[:WORKING_SUMMARY_MAX]

    # 8) 推进折叠边界（折叠成功或失败都推进，避免无限累积）
    if fold_advance_to is not None:
        summary_row.buffer_start_id = fold_advance_to

    # 9) 推进水位线（无论抽取成败，避免同一批无限重试）
    summary_row.last_processed_id = new_records[-1].id
    await summary_row.save()


async def _collect_fold_records(user_id: int, chat_group_id: int, summary_row):
    """找出需要折入工作摘要的早期消息，返回 (overflow_section, 新的 buffer_start_id)。

    语义：最近 WORKING_BUFFER_TURNS 条保留原文（供 Brain 水合），
    之前的、且尚未折叠（>= buffer_start_id）的消息一起折入滚动摘要。
    """
    all_ids = await ChatHistory.filter(
        user_id=user_id, chat_group_id=chat_group_id
    ).order_by("id").values_list("id", flat=True)
    if not all_ids:
        return "", None

    keep_from = all_ids[-WORKING_BUFFER_TURNS] if len(all_ids) > WORKING_BUFFER_TURNS else all_ids[0]

    fold_qs = ChatHistory.filter(user_id=user_id, chat_group_id=chat_group_id, id__lt=keep_from)
    if summary_row.buffer_start_id:
        fold_qs = fold_qs.filter(id__gte=summary_row.buffer_start_id)
    fold_records = await fold_qs.order_by("id").all()
    if not fold_records:
        return "", keep_from

    section = "以下是本次需要折叠进工作摘要的早期对话：\n" + _format_turns(fold_records)
    return section, keep_from


async def _existing_facts_json(user_id: int) -> str:
    """把现有 user 级 KV 打包成 prompt 里的 {existing_facts}。"""
    rows = await MemoryKV.filter(user_id=user_id, scope="user").order_by("-updated_at").limit(20).all()
    if not rows:
        return "无"
    facts = [{"key": r.key, "value": r.value[:120], "confidence": round(r.confidence, 2)} for r in rows]
    try:
        return json.dumps(facts, ensure_ascii=False)
    except Exception:
        logger.warning("existing_facts 序列化失败 user=%s", user_id, exc_info=True)
        return "无"


# ---------------------------------------------------------------------------
# KV 落库（冲突处理）
# ---------------------------------------------------------------------------

async def _upsert_kvs(user_id: int, chat_group_id: int, agent_id: int | None, kv_list: list):
    if not kv_list or not isinstance(kv_list, list):
        return
    for kv in kv_list:
        if not isinstance(kv, dict):
            continue
        key = (str(kv.get("key") or "")).strip()[:64]
        value = (str(kv.get("value") or "")).strip()[:1000]
        if not key or not value:
            continue
        scope = kv.get("scope") if kv.get("scope") in ("user", "group") else "user"
        new_conf = float(kv.get("confidence", 0.5))
        source = str(kv.get("source") or "agent_inferred")

        existing = await MemoryKV.filter(user_id=user_id, key=key, scope=scope).first()
        if existing:
            # 高置信 user_stated 旧值 + 低置信新值 => 保留旧值（用户亲口说的优先）
            if existing.source == "user_stated" and new_conf < 0.5:
                continue
            existing.value = value
            existing.confidence = min(0.95, existing.confidence * 0.6 + new_conf * 0.4)
            existing.version += 1
            existing.source_group_id = chat_group_id
            existing.source_agent_id = agent_id
            existing.subjects = kv.get("subjects") or existing.subjects
            await existing.save()
        else:
            await MemoryKV.create(
                user_id=user_id, key=key, value=value, scope=scope,
                subjects=kv.get("subjects"), source_group_id=chat_group_id,
                source_agent_id=agent_id, source=source, confidence=min(0.95, new_conf),
            )

    await _evict_kv_if_over(user_id)


async def _evict_kv_if_over(user_id: int):
    total = await MemoryKV.filter(user_id=user_id, scope="user").count()
    if total <= MAX_KV_PER_USER:
        return
    over = total - MAX_KV_PER_USER
    rows = await MemoryKV.filter(user_id=user_id, scope="user").order_by("confidence", "updated_at").limit(over).all()
    for r in rows:
        await r.delete()


# ---------------------------------------------------------------------------
# 情景记忆合并
# ---------------------------------------------------------------------------

async def _merge_episode(user_id: int, chat_group_id: int, agent_id: int | None, data, new_records):
    ep, _ = await MemoryEpisode.get_or_create(user_id=user_id, chat_group_id=chat_group_id)
    now = datetime.now()

    new_summary = data.get("episode_summary")
    if isinstance(new_summary, str) and new_summary.strip():
        ep.summary = new_summary.strip()

    subjects = data.get("subjects") or []
    if isinstance(subjects, list) and subjects:
        ep.subjects = sorted(set((ep.subjects or []) + [str(s)[:32] for s in subjects]))

    try:
        imp = float(data.get("importance", 0.5))
        ep.importance = max(ep.importance, min(1.0, imp))
    except (TypeError, ValueError):
        logger.debug("episode importance 非数字，忽略 user=%s value=%r", user_id, data.get("importance"))

    ep.end_time = now
    if not ep.start_time:
        ep.start_time = now
    ep.turn_count += len(new_records)

    if ep.summary:
        try:
            vec = await encode(ep.summary[:500])
            ep.embedding = json.dumps(vec.tolist(), ensure_ascii=False)
        except Exception:
            logger.exception("episode embedding failed user=%s", user_id)
    await ep.save()


# ---------------------------------------------------------------------------
# 重要原文入向量索引
# ---------------------------------------------------------------------------

async def _index_messages(user_id: int, chat_group_id: int, agent_id: int | None, new_records, data):
    picks = {}
    for m in (data.get("index_messages") or []):
        if isinstance(m, dict) and m.get("chat_history_id"):
            picks[int(m["chat_history_id"])] = m

    for r in new_records:
        pick = picks.get(r.id)
        # 只索引：LLM 挑出的重要消息，或足够长的用户提问
        if not pick and (not r.req or len(r.req.strip()) < 8):
            continue
        content = (pick.get("text") if pick else None) or r.req or ""
        content = str(content).strip()[:500]
        if not content:
            continue
        try:
            vec = await encode(content)
        except Exception:
            logger.exception("message embedding failed user=%s", user_id)
            continue
        await MemoryMessage.create(
            user_id=user_id, chat_group_id=chat_group_id,
            source_history_id=r.id, role="user", content=content,
            embedding=json.dumps(vec.tolist(), ensure_ascii=False),
            subjects=(pick.get("subjects") if pick else None) or data.get("subjects"),
            importance=float(pick.get("importance", 0.5)) if pick else 0.5,
        )

    await _evict_messages_if_over(user_id)


async def _evict_messages_if_over(user_id: int):
    total = await MemoryMessage.filter(user_id=user_id).count()
    if total <= MAX_MESSAGES_PER_USER:
        return
    over = total - MAX_MESSAGES_PER_USER
    rows = await MemoryMessage.filter(user_id=user_id).order_by("created_at").limit(over).all()
    for r in rows:
        await r.delete()


# ---------------------------------------------------------------------------
# 给 get_used_history 用的紧凑历史预览
# ---------------------------------------------------------------------------

async def build_history_preview(user_id: int, chat_group_id: int, query: str = "", max_chars: int = 1500) -> str:
    """返回最近 10 轮紧凑文本；带 query 时叠加语义检索的 top-K 原文。

    替代旧的"整组 dump"，封住 token 成本。保持工具名兼容。
    """
    from backend.src.service.memory.retrieval import retrieve_messages

    records = await ChatHistory.filter(
        user_id=user_id, chat_group_id=chat_group_id
    ).order_by("-id").limit(10).all()
    records = list(reversed(records))
    if not records:
        return "当前聊天组暂无历史记录"

    text = "【最近对话摘要】\n" + _format_turns(records)
    text = text[:max_chars]

    if query:
        try:
            hits = await retrieve_messages(user_id, chat_group_id, query, top_k=2)
            if hits:
                extra = "\n\n【按当前问题检索到的相关历史原话】\n" + "\n".join(
                    f"- {h['content'][:120]}" for h in hits
                )
                text = (text + extra)[:max_chars]
        except Exception:
            logger.exception("history preview semantic recall failed user=%s", user_id)

    return text
