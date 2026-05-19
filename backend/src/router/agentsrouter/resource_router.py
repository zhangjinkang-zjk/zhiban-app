from fastapi import APIRouter, HTTPException, Depends, Body
from fastapi.responses import StreamingResponse

from backend.src.service.agentsservice.resource_service import ResourceService
from backend.src.service.agentsservice.skill_service import SkillService
from backend.src.pojo.resourcepojo import GenerateResourceRequest
from backend.src.pojo.skillpojo import UpsertSkillRequest
from backend.src.utils.jwt import get_user_id_from_token

router = APIRouter(prefix = "/resource", tags = ["学习资源生成"])


# ═══════════════════════════════════════
#  生成
# ═══════════════════════════════════════

@router.post("/generate")
async def generate_resource(
    user_id : int = Depends(get_user_id_from_token),
    data : GenerateResourceRequest = Body(...)
):
    try :
        result = await ResourceService.generate_and_save(data.topic, user_id, data.resource_types)
        return {"code" : 200, "msg" : "success", "data" : result}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.post("/generate/stream")
async def generate_resource_stream(
    user_id : int = Depends(get_user_id_from_token),
    data : GenerateResourceRequest = Body(...)
):
    return StreamingResponse(
        ResourceService.generate_stream(data.topic, user_id, data.resource_types),
        media_type = "text/event-stream",
    )


@router.get("/list")
async def list_resources(user_id : int = Depends(get_user_id_from_token)):
    try :
        records = await ResourceService.list_resources(user_id)
        return {"code" : 200, "msg" : "success", "data" : records}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


# ═══════════════════════════════════════
#  Skill 管理
# ═══════════════════════════════════════

@router.post("/skill/upsert")
async def upsert_skill(
    user_id : int = Depends(get_user_id_from_token),
    data : UpsertSkillRequest = Body(...)
):
    try :
        result = await SkillService.upsert(user_id, data.resource_type, data.name, data.system_prompt)
        return {"code" : 200, "msg" : "success", "data" : result}
    except ValueError as e :
        return {"code" : 400, "msg" : str(e)}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.get("/skill/list")
async def list_skills(user_id : int = Depends(get_user_id_from_token)):
    try :
        records = await SkillService.list_all(user_id)
        return {"code" : 200, "msg" : "success", "data" : records}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.get("/skill/{resource_type}")
async def get_skill(
    resource_type : str,
    user_id : int = Depends(get_user_id_from_token)
):
    try :
        record = await SkillService.get(user_id, resource_type)
        if record is None :
            return {"code" : 404, "msg" : f"资源类型 '{resource_type}' 暂无定制 skill"}
        return {"code" : 200, "msg" : "success", "data" : record}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.delete("/skill/{resource_type}")
async def delete_skill(
    resource_type : str,
    user_id : int = Depends(get_user_id_from_token)
):
    try :
        msg = await SkillService.delete(user_id, resource_type)
        if "不存在" in msg :
            return {"code" : 404, "msg" : msg}
        return {"code" : 200, "msg" : msg}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


# ═══════════════════════════════════════
#  资源 CRUD
# ═══════════════════════════════════════

@router.get("/{resource_id}")
async def get_resource(
    resource_id : int,
    user_id : int = Depends(get_user_id_from_token)
):
    try :
        record = await ResourceService.get_resource(resource_id, user_id)
        if record is None :
            return {"code" : 404, "msg" : "资源不存在"}
        return {"code" : 200, "msg" : "success", "data" : record}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.get("/{resource_id}/download")
async def download_resource(
    resource_id : int,
    user_id : int = Depends(get_user_id_from_token)
):
    try :
        result = await ResourceService.download_resource(resource_id, user_id)
        if result is None :
            return {"code" : 404, "msg" : "资源不存在"}
        content, filename, media_type = result
        return StreamingResponse(
            iter([content]),
            media_type = media_type,
            headers = {"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")


@router.delete("/{resource_id}")
async def delete_resource(
    resource_id : int,
    user_id : int = Depends(get_user_id_from_token)
):
    try :
        ok = await ResourceService.delete_resource(resource_id, user_id)
        if not ok :
            return {"code" : 404, "msg" : "资源不存在"}
        return {"code" : 200, "msg" : "删除成功"}
    except HTTPException :
        raise
    except Exception :
        raise HTTPException(500, "服务器错误")
