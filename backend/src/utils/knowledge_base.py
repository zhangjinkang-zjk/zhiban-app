"""
知识库 — 向量数据存储于 MySQL，支持用户隔离和公开/私有权限
BGE 模型仍从本地加载（开源模型，不包含用户数据）
"""
import os
import json
import hashlib
import asyncio
import logging
from pathlib import Path

HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")
if HF_ENDPOINT:
    os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)

from tortoise.expressions import Q

from backend.src.models.knowledgemodel import KnowledgeVector

logger = logging.getLogger(__name__)

# BGE 模型本地缓存路径 — 第一次使用时才加载，避免 import 时拖慢启动
MODEL_DIR = str(Path(__file__).parent.parent / "ai_core" / "knowledge_base" / "bge_model")
_embed_model = None
_embed_lock = asyncio.Lock()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_RAG_SEARCH_SEM = asyncio.Semaphore(max(1, _int_env("RAG_GLOBAL_SEARCH_CONCURRENCY", 20)))


def _make_doc_id(title: str, content: str, scope: str = "") -> str:
    raw = f"{scope}|{title}|{content[:100]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _get_embed_model_async():
    """异步加载 BGE 模型（首次使用时才 import sentence_transformers，避免拖慢启动）"""
    global _embed_model
    if _embed_model is not None:
        return _embed_model

    async with _embed_lock:
        if _embed_model is not None:
            return _embed_model
        from sentence_transformers import SentenceTransformer
        local_path = Path(MODEL_DIR)
        if local_path.exists() and any(local_path.iterdir()):
            _embed_model = await asyncio.to_thread(SentenceTransformer, str(local_path))
        else:
            _embed_model = await asyncio.to_thread(SentenceTransformer, "BAAI/bge-small-zh-v1.5")
            await asyncio.to_thread(_embed_model.save, str(local_path))
        return _embed_model


async def _encode_async(text: str):
    """异步编码文本为向量（Redis 缓存 + 线程池降级）"""
    import numpy as np

    # Redis 缓存：相同文本 24 小时内不重复计算向量
    cache_key = None
    cache_ttl = None
    if text and len(text.strip()) > 2:
        try:
            from backend.src.utils.redis_client import cache_get, cache_set, _cache_key, _text_hash
            from backend.src.utils.constants import EMBED_CACHE_TTL
            cache_key = _cache_key("embed", _text_hash(text.strip()))
            cache_ttl = EMBED_CACHE_TTL
            cached = await cache_get(cache_key)
            if cached is not None and isinstance(cached, list):
                return np.array(cached, dtype=np.float32)
        except Exception:
            logger.debug("Embedding cache read failed", exc_info=True)

    model = await _get_embed_model_async()
    vector = await asyncio.to_thread(model.encode, text, normalize_embeddings=True)

    # 异步回填缓存（不阻塞返回）
    if cache_key and cache_ttl:
        try:
            from backend.src.utils.redis_client import cache_set
            await cache_set(cache_key, vector.tolist(), cache_ttl)
        except Exception:
            logger.debug("Embedding cache write failed", exc_info=True)

    return vector


async def search(query: str, top_k: int = 5, user_id: int = None, category: str = None) -> str:
    async with _RAG_SEARCH_SEM:
        return await _search_inner(query, top_k=top_k, user_id=user_id, category=category)


async def _search_inner(query: str, top_k: int = 5, user_id: int = None, category: str = None) -> str:
    """
    从知识库检索资料。
    - user_id 为空：只查公开资料
    - user_id 不为空：查公开资料 + 该用户自己的私有资料
    - category: 限定分类，如 "exercise" / "textbook"
    """
    try:
        import numpy as np
        query_vec = await _encode_async(query)
        min_score = _float_env("RAG_MIN_SCORE", 0.0)
        max_chars = _int_env("RAG_RESULT_MAX_CHARS", 1200)

        if user_id:
            qs = KnowledgeVector.filter(Q(visibility="public") | Q(user_id=user_id))
        else:
            qs = KnowledgeVector.filter(visibility="public")

        if category:
            qs = qs.filter(category=category)

        records = await qs.values("doc_id", "title", "content", "category", "embedding")

        if not records:
            return "知识库中暂无相关内容"

        scored = []
        for r in records:
            raw_embedding = r.get("embedding") or "[]"
            try:
                embedding = json.loads(raw_embedding)
                if not embedding:
                    continue
                vec = np.array(embedding, dtype=np.float32)
                if vec.shape != query_vec.shape:
                    continue
            except Exception:
                continue
            sim = float(np.dot(query_vec, vec))
            if sim < min_score:
                continue
            scored.append((sim, r.get("doc_id", ""), r["title"], r["content"], r.get("category", "")))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return "知识库中暂无相关内容"

        items = [
            (
                f"【资料{i+1}】来源：{title}（{cat}，score={sim:.3f}，doc_id={doc_id}）\n"
                f"{str(content or '')[:max_chars]}\n"
            )
            for i, (sim, doc_id, title, content, cat) in enumerate(scored[:top_k])
        ]
        return "\n".join(items)

    except Exception as e:
        return f"知识库检索失败：{e}"


async def ingest(
    title: str,
    content: str,
    user_id: int = None,
    visibility: str = "private",
    category: str = "knowledge_point",
    cover_url: str | None = None,
) -> str:
    """
    向知识库添加一条资料。
    - user_id=None: 系统上传（需管理员权限）
    - visibility='public': 全员可见; 'private': 仅上传者可见
    - category: 见 KB_CATEGORIES
    - cover_url: 可选封面图 URL，不传则按 category 使用默认封面
    """
    try:
        if len(content.strip()) < 50:
            return "内容过短（<50字），未入库"

        doc_id = _make_doc_id(title, content)
        vector = await _encode_async(content)

        existing = await KnowledgeVector.filter(doc_id=doc_id).first()
        if existing:
            same_owner = existing.user_id == user_id
            same_system_scope = existing.user_id is None and user_id is None
            globally_reusable = existing.visibility == "public" and visibility == "public"
            if not (same_owner or same_system_scope or globally_reusable):
                doc_id = _make_doc_id(title, content, scope=f"user:{user_id or 'system'}:{visibility}")
                existing = await KnowledgeVector.filter(doc_id=doc_id).first()

        if existing:
            updated = False
            if existing.visibility == "private" and visibility in {"public", "pending"}:
                existing.visibility = visibility
                updated = True
            if existing.user_id is None and user_id is not None and existing.visibility != "public":
                existing.user_id = user_id
                updated = True
            if category != existing.category:
                existing.category = category
                updated = True
            if updated:
                await existing.save()
                return f"「{title}」已存在，已更新权限"
            return f"「{title}」已存在，跳过"

        if not cover_url:
            category_cover_map = {
                "knowledge_point": "document",
                "exercise": "exercise",
                "textbook": "document",
                "note": "document",
                "case_study": "document",
                "reference": "document",
            }
            cover_url = f"/static/covers/default_{category_cover_map.get(category, '')}.svg"

        await KnowledgeVector.create(
            doc_id=doc_id,
            title=title,
            content=content,
            embedding=json.dumps(vector.tolist()),
            user_id=user_id,
            visibility=visibility,
            category=category,
            cover_url=cover_url,
        )
        label = "公开" if visibility == "public" else "待审核" if visibility == "pending" else "私有"
        return f"「{title}」已入库（{len(content)}字，{label}，{category}）"

    except Exception as e:
        return f"入库失败：{e}"


async def list_all(user_id: int = None, visibility: str = None) -> list[dict]:
    """
    列出知识库条目（原始切片）。
    - user_id: 过滤上传者（可选）
    - visibility: 过滤可见性（可选）
    """
    qs = KnowledgeVector.all()
    if user_id:
        qs = qs.filter(Q(visibility="public") | Q(user_id=user_id))
    if visibility:
        qs = qs.filter(visibility=visibility)

    records = await qs.order_by("-created_at").values(
        "doc_id", "title", "content", "category", "user_id", "visibility", "cover_url", "created_at"
    )
    return list(records)


def _merge_overlapped_chunks(chunks: list[str]) -> str:
    parts = [str(chunk or "").strip() for chunk in chunks if str(chunk or "").strip()]
    if not parts:
        return ""

    merged = parts[0]
    for chunk in parts[1:]:
        max_overlap = min(220, len(merged), len(chunk))
        overlap = 0
        for size in range(max_overlap, 39, -1):
            if merged.endswith(chunk[:size]):
                overlap = size
                break
        merged = f"{merged}{chunk[overlap:]}" if overlap else f"{merged}\n\n{chunk}"
    return merged


async def list_grouped(user_id: int = None, visibility: str = None) -> list[dict]:
    """
    按原始文档分组展示，合并 BGE 切片避免前端展示混乱。
    切片标题格式: "文档名 (第N部分)" → 按 "文档名" 合并
    """
    import re

    qs = KnowledgeVector.all()
    if user_id:
        qs = qs.filter(Q(visibility="public") | Q(user_id=user_id))
    if visibility:
        qs = qs.filter(visibility=visibility)

    records = await qs.order_by("-created_at").values(
        "doc_id", "title", "content", "category", "user_id", "visibility", "cover_url", "created_at"
    )

    def _chunk_index(title: str) -> int:
        match = re.search(r"[（(]第(\d+)部分[）)]", title or "")
        return int(match.group(1)) if match else 1

    groups: dict[str, dict] = {}
    for r in records:
        title = r["title"]
        base = re.sub(r"\s*（第\d+部分）\s*", "", title)
        base = re.sub(r"\s*\(第\d+部分\)\s*", "", base)

        if base not in groups:
            groups[base] = {
                "title": base,
                "category": r.get("category", "knowledge_point"),
                "doc_id": r["doc_id"],
                "doc_ids": [],
                "_parts": [],
                "chunks": 0,
                "total_chars": 0,
                "preview": r["content"][:200],
                "content": "",
                "visibility": r["visibility"],
                "uploader_id": r["user_id"],
                "cover_url": r.get("cover_url"),
                "created_at": str(r["created_at"]),
            }
        groups[base]["_parts"].append({
            "index": _chunk_index(title),
            "doc_id": r["doc_id"],
            "content": r["content"],
        })
        groups[base]["chunks"] += 1
        groups[base]["total_chars"] += len(r["content"])
        if str(r["created_at"]) < groups[base]["created_at"]:
            groups[base]["created_at"] = str(r["created_at"])

    result = []
    for group in groups.values():
        parts = sorted(group.pop("_parts", []), key=lambda item: item["index"])
        group["doc_ids"] = [item["doc_id"] for item in parts]
        group["content"] = _merge_overlapped_chunks([item["content"] for item in parts])
        if group["doc_ids"]:
            group["doc_id"] = group["doc_ids"][0]
        result.append(group)

    return sorted(result, key=lambda x: x["created_at"], reverse=True)


async def get_by_id(doc_id: str) -> dict | None:
    """根据 doc_id 获取单条知识库记录"""
    record = await KnowledgeVector.filter(doc_id=doc_id).values(
        "doc_id", "title", "content", "category", "user_id", "visibility", "cover_url", "created_at"
    ).first()
    return record


async def update(
    doc_id: str,
    title: str = None,
    content: str = None,
    visibility: str = None,
    user_id: int = None,
    is_admin: bool = False,
) -> str:
    """
    更新知识库条目。
    - 仅 owner 或 admin 可操作
    - content 变更会自动重新嵌入向量
    """
    try:
        record = await KnowledgeVector.filter(doc_id=doc_id).first()
        if not record:
            return "记录不存在"

        # ── 权限：仅 owner 或 admin ──
        if record.user_id is not None and record.user_id != user_id and not is_admin:
            return "无权修改他人私有资料"

        # ── 系统资料仅 admin ──
        if record.user_id is None and not is_admin:
            return "无权修改系统公开资料"

        if title is not None:
            record.title = title
        if visibility is not None:
            record.visibility = visibility

        # 内容变更 → 重新嵌入
        if content is not None:
            if len(content.strip()) < 50:
                return "内容过短（<50字），更新失败"
            new_doc_id = _make_doc_id(title or record.title, content, scope=f"user:{user_id or 'system'}")
            vector = await _encode_async(content)
            record.doc_id = new_doc_id
            record.content = content
            record.embedding = json.dumps(vector.tolist())

        await record.save()
        return f"「{record.title}」已更新"
    except Exception as e:
        return f"更新失败：{e}"


async def delete(doc_id: str, user_id: int = None, is_admin: bool = False) -> str:
    """
    删除知识库条目。
    - 仅 owner 或 admin 可操作
    """
    try:
        record = await KnowledgeVector.filter(doc_id=doc_id).first()
        if not record:
            return "记录不存在"

        if record.user_id is not None and record.user_id != user_id and not is_admin:
            return "无权删除他人私有资料"
        if record.user_id is None and not is_admin:
            return "无权删除系统公开资料"

        title = record.title
        await record.delete()
        return f"「{title}」已删除"
    except Exception as e:
        return f"删除失败：{e}"
