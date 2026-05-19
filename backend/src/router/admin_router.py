from fastapi import APIRouter, HTTPException, Depends, Body

from backend.src.service.adminservice.admin_service import AdminService
from backend.src.pojo.adminpojo import ResetPasswordRequest, DeleteUserRequest
from backend.src.utils.jwt import get_user_id_from_token
from backend.src.utils.admin_check import is_admin

router = APIRouter(prefix = "/admin", tags = ["管理员"])


async def _require_admin(user_id : int = Depends(get_user_id_from_token)) -> int:
    if not await is_admin(user_id):
        raise HTTPException(403, "需要管理员权限")
    return user_id


@router.get("/users")
async def list_users(admin_id : int = Depends(_require_admin)):
    try :
        users = await AdminService.list_users()
        return {"code" : 200, "msg" : "success", "data" : users}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.delete("/users/{user_id}")
async def delete_user(
    user_id : int,
    admin_id : int = Depends(_require_admin),
    data : DeleteUserRequest = Body(...)
):
    if not data.confirm :
        return {"code" : 400, "msg" : "请确认删除"}
    try :
        msg = await AdminService.delete_user(user_id)
        if "不存在" in msg or "不能删除" in msg :
            return {"code" : 403, "msg" : msg}
        return {"code" : 200, "msg" : msg}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.post("/users/{user_id}/reset_password")
async def reset_password(
    user_id : int,
    admin_id : int = Depends(_require_admin),
    data : ResetPasswordRequest = Body(...)
):
    try :
        msg = await AdminService.reset_password(user_id, data.new_password)
        if "不存在" in msg :
            return {"code" : 404, "msg" : msg}
        return {"code" : 200, "msg" : msg}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.get("/knowledge_base")
async def admin_knowledge_base(admin_id : int = Depends(_require_admin)):
    try :
        records = await AdminService.list_knowledge_base()
        return {"code" : 200, "msg" : "success", "data" : records}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")
