from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.src.service.narration import service as narration_service
from backend.src.utils.jwt import get_user_id_from_token
from backend.src.utils.tts_utils import VOICES


DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

router = APIRouter(prefix="/narration", tags=["资源旁白"])
legacy_video_router = APIRouter(prefix="/video", tags=["资源旁白兼容"])


class NarrateRequest(BaseModel):
    resource_id: int = Field(..., ge=1)
    voice: str = Field(default=DEFAULT_VOICE)
    force_regenerate: bool = False


def _ok(data=None, msg: str = "success") -> dict:
    return {"code": 200, "msg": msg, "data": data}


def _voices_payload() -> dict:
    return {
        "default": DEFAULT_VOICE,
        "voices": [{"name": voice, "value": voice} for voice in VOICES],
    }


async def _narrate_resource(
    data: NarrateRequest,
    user_id: int,
) -> dict:
    result = await narration_service.narrate_resource(
        resource_id=data.resource_id,
        voice=data.voice or DEFAULT_VOICE,
        force_regenerate=data.force_regenerate,
        user_id=user_id,
    )
    return _ok(result)


@router.get("/voices")
@legacy_video_router.get("/voices")
async def list_voices():
    return _ok(_voices_payload())


@router.post("/narrate")
@legacy_video_router.post("/narrate")
async def narrate(
    data: NarrateRequest,
    user_id: int = Depends(get_user_id_from_token),
):
    return await _narrate_resource(data, user_id)


@router.get("/list")
async def list_narrations(user_id: int = Depends(get_user_id_from_token)):
    return _ok(await narration_service.list_narrations(user_id))


@router.get("/{narration_id}")
@legacy_video_router.get("/narrations/{narration_id}")
async def get_narration(
    narration_id: int,
    user_id: int = Depends(get_user_id_from_token),
):
    result = await narration_service.get_narration(narration_id, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="旁白不存在或无访问权限")
    return _ok(result)


@router.delete("/{narration_id}")
async def delete_narration(
    narration_id: int,
    user_id: int = Depends(get_user_id_from_token),
):
    deleted = await narration_service.delete_narration(narration_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="旁白不存在或无访问权限")
    return _ok({"deleted": True})
