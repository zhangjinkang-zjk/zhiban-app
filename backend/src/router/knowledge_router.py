import traceback
from pathlib import Path
import tempfile
import os

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query

from backend.src.utils.jwt import get_user_id_from_token
from backend.src.utils.file_processor import extract_text, chunk_text
from backend.src.utils.knowledge_base import ingest, list_all, list_grouped, get_by_id, update, delete
from backend.src.utils.admin_check import is_admin

router = APIRouter(prefix="/knowledge_base", tags=["知识库"])

ALLOWED_EXTENSIONS = {".txt", ".pdf", ".docx"}


@router.post("/upload")
async def upload_document(
    user_id: int = Depends(get_user_id_from_token),
    file: UploadFile = File(...),
    title: str = Form(None),
    visibility: str = Form("private"),
    category: str = Form("knowledge_point"),
):
    """
    上传文档到知识库。
    - visibility='private'（默认）：仅上传者可见
    - visibility='public'：需管理员权限，全员可见
    - category: 前端自定义分类字符串
    """
    tmp_path = None
    try:
        # ── 公开上传需管理员 ──
        if visibility == "public":
            if not await is_admin(user_id):
                return {
                    "code": 403,
                    "msg": "仅管理员可上传公开资料",
                }

        # ── 校验后缀 ──
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            return {
                "code": 400,
                "msg": f"不支持的文件格式 {suffix}，仅支持 {', '.join(ALLOWED_EXTENSIONS)}",
            }

        # ── 保存临时文件 ──
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content_bytes = await file.read()
            tmp.write(content_bytes)
            tmp_path = tmp.name

        # ── 提取文本 ──
        raw_text = extract_text(tmp_path)
        if not raw_text.strip():
            return {"code": 400, "msg": "文件内容为空"}

        # ── 标题 ──
        doc_title = title.strip() if title else Path(file.filename).stem

        # ── 切片 + 逐块入库 ──
        chunks = chunk_text(raw_text, max_chars=500)
        results = []
        for idx, chunk in enumerate(chunks):
            chunk_title = f"{doc_title} (第{idx+1}部分)" if len(chunks) > 1 else doc_title
            msg = await ingest(
                title=chunk_title,
                content=chunk,
                user_id=user_id,
                visibility=visibility,
                category=category,
            )
            results.append(msg)

        success = sum(1 for r in results if "已入库" in r)
        skipped = sum(1 for r in results if "跳过" in r or "过短" in r)

        return {
            "code": 200,
            "msg": f"处理完成：共 {len(chunks)} 段，入库 {success} 段，跳过 {skipped} 段",
            "data": {
                "filename": file.filename,
                "title": doc_title,
                "visibility": visibility,
                "total_chunks": len(chunks),
                "ingested": success,
                "skipped": skipped,
                "details": results,
            },
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"上传失败: {type(e).__name__}: {e}")

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ═══════════════════════════════════════
#  CRUD
# ═══════════════════════════════════════

@router.get("/list")
async def list_entries(
    user_id: int = Depends(get_user_id_from_token),
    visibility: str = Query(None),
    mine: bool = Query(False),
):
    """列出知识库条目。mine=true 只看自己的，visibility 过滤公开/私有"""
    try:
        filter_user = user_id if mine else None
        records = await list_grouped(user_id=filter_user, visibility=visibility)
        return {"code": 200, "msg": "success", "data": records}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/{doc_id}")
async def get_entry(doc_id: str):
    """获取单条知识库记录"""
    try:
        record = await get_by_id(doc_id)
        if not record:
            return {"code": 404, "msg": "记录不存在"}
        return {"code": 200, "msg": "success", "data": record}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.put("/{doc_id}")
async def update_entry(
    doc_id: str,
    user_id: int = Depends(get_user_id_from_token),
    title: str = Form(None),
    content: str = Form(None),
    visibility: str = Form(None),
):
    """更新知识库条目。管理员可改任意，普通用户只能改自己的"""
    try:
        admin = await is_admin(user_id)
        msg = await update(
            doc_id=doc_id,
            title=title,
            content=content,
            visibility=visibility,
            user_id=user_id,
            is_admin=admin,
        )
        if "失败" in msg or "无权" in msg:
            return {"code": 403, "msg": msg}
        return {"code": 200, "msg": msg}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/{doc_id}")
async def delete_entry(
    doc_id: str,
    user_id: int = Depends(get_user_id_from_token),
):
    """删除知识库条目。管理员可删任意，普通用户只能删自己的"""
    try:
        admin = await is_admin(user_id)
        msg = await delete(doc_id=doc_id, user_id=user_id, is_admin=admin)
        if "失败" in msg or "无权" in msg:
            return {"code": 403, "msg": msg}
        return {"code": 200, "msg": msg}
    except Exception as e:
        raise HTTPException(500, str(e))
