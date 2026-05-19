"""
知识库 — 向量数据存储于 MySQL，支持用户隔离和公开/私有权限
BGE 模型仍从本地加载（开源模型，不包含用户数据）
"""
import json
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from tortoise.expressions import Q

from backend.src.models.knowledgemodel import KnowledgeVector

# BGE 模型本地缓存路径 — 第一次会自动下载到这里，后续离线加载
MODEL_DIR = str(Path(__file__).parent.parent / "ai_core" / "knowledge_base" / "bge_model")
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is not None:
        return _embed_model

    local_path = Path(MODEL_DIR)
    if local_path.exists() and any(local_path.iterdir()):
        _embed_model = SentenceTransformer(str(local_path))
    else:
        _embed_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        _embed_model.save(str(local_path))
    return _embed_model


async def search(query: str, top_k: int = 5, user_id: int = None, category: str = None) -> str:
    """
    从知识库检索资料。
    - user_id 为空：只查公开资料
    - user_id 不为空：查公开资料 + 该用户自己的私有资料
    - category: 限定分类，如 "exercise" / "textbook"
    """
    try:
        model = _get_embed_model()
        query_vec = model.encode(query, normalize_embeddings=True)

        if user_id:
            qs = KnowledgeVector.filter(Q(visibility="public") | Q(user_id=user_id))
        else:
            qs = KnowledgeVector.filter(visibility="public")

        if category:
            qs = qs.filter(category=category)

        records = await qs.values("title", "content", "category", "embedding")

        if not records:
            return "知识库中暂无相关内容"

        scored = []
        for r in records:
            vec = np.array(json.loads(r["embedding"]), dtype=np.float32)
            sim = float(np.dot(query_vec, vec))
            scored.append((sim, r["title"], r["content"], r.get("category", "")))

        scored.sort(key=lambda x: x[0], reverse=True)

        items = [
            f"【资料{i+1}】来源：{title}（{cat}）\n{content}\n"
            for i, (_, title, content, cat) in enumerate(scored[:top_k])
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
) -> str:
    """
    向知识库添加一条资料。
    - user_id=None: 系统上传（需管理员权限）
    - visibility='public': 全员可见; 'private': 仅上传者可见
    - category: 见 KB_CATEGORIES
    """
    try:
        if len(content.strip()) < 50:
            return "内容过短（<50字），未入库"

        model = _get_embed_model()
        doc_id = str(hash(title + content[:100]))
        vector = model.encode(content, normalize_embeddings=True)

        existing = await KnowledgeVector.filter(doc_id=doc_id).first()
        if existing:
            updated = False
            if existing.visibility == "private" and visibility == "public":
                existing.visibility = "public"
                updated = True
            if existing.user_id is None and user_id is not None:
                existing.user_id = user_id
                updated = True
            if category != existing.category:
                existing.category = category
                updated = True
            if updated:
                await existing.save()
                return f"「{title}」已存在，已更新权限"
            return f"「{title}」已存在，跳过"

        await KnowledgeVector.create(
            doc_id=doc_id,
            title=title,
            content=content,
            embedding=json.dumps(vector.tolist()),
            user_id=user_id,
            visibility=visibility,
            category=category,
        )
        label = "公开" if visibility == "public" else "私有"
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
        "doc_id", "title", "content", "category", "user_id", "visibility", "created_at"
    )
    return list(records)


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
        "doc_id", "title", "content", "category", "user_id", "visibility", "created_at"
    )

    groups: dict[str, dict] = {}
    for r in records:
        title = r["title"]
        base = re.sub(r"\s*（第\d+部分）\s*", "", title)
        base = re.sub(r"\s*\(第\d+部分\)\s*", "", base)

        if base not in groups:
            groups[base] = {
                "title": base,
                "category": r.get("category", "knowledge_point"),
                "chunks": 0,
                "total_chars": 0,
                "preview": r["content"][:200],
                "visibility": r["visibility"],
                "uploader_id": r["user_id"],
                "created_at": str(r["created_at"]),
            }
        groups[base]["chunks"] += 1
        groups[base]["total_chars"] += len(r["content"])
        if str(r["created_at"]) < groups[base]["created_at"]:
            groups[base]["created_at"] = str(r["created_at"])

    return sorted(groups.values(), key=lambda x: x["created_at"], reverse=True)


async def get_by_id(doc_id: str) -> dict | None:
    """根据 doc_id 获取单条知识库记录"""
    record = await KnowledgeVector.filter(doc_id=doc_id).first().values(
        "doc_id", "title", "content", "category", "user_id", "visibility", "created_at"
    )
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
            model = _get_embed_model()
            new_doc_id = str(hash(title or record.title + content[:100]))
            vector = model.encode(content, normalize_embeddings=True)
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
