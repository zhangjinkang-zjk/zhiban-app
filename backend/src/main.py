import asyncio
import logging
import sys
from pathlib import Path

# 确保 backend/ 的父目录在 sys.path，这样无论从项目根还是 backend/ 内启动 uvicorn 都能正确导入 backend.src.xxx
_sys_root = Path(__file__).resolve().parent.parent.parent
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

# ── 项目唯一 .env 加载点 ──
from dotenv import load_dotenv
_backend_root = Path(__file__).resolve().parent.parent  # backend/
load_dotenv(_backend_root / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from backend.src.utils.database import init_db, close_db

logger = logging.getLogger("api")

# Redis SSE 转发器任务句柄（在 startup 中赋值，shutdown 中清理）
_redis_forward_task = None
from backend.src.router.chat_router import router as chat_router
from backend.src.router.portrait_router import router as portrait_router
from backend.src.router.resource_router import router as resource_router
from backend.src.router.image_router import router as image_router
from backend.src.router.knowledge_router import router as knowledge_router
from backend.src.router.user_router import router as user_router
from backend.src.router.admin_router import router as admin_router
from backend.src.router.exam_router import router as exam_router
from backend.src.router.path_router import router as path_router
from backend.src.router.learning_path_router import router as learning_path_router
from backend.src.router.narration_router import router as narration_router
from backend.src.router.narration_router import legacy_video_router as legacy_video_narration_router
from backend.src.router.video_router import router as video_router
from backend.src.router.study_router import router as study_router
from backend.src.router.notification_router import router as notification_router
from backend.src.router.annotation_router import router as annotation_router
app = FastAPI(
    title="AI聊天后端",
    description="Swagger接口文档",
    swagger_ui_init_oauth={},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"https://.*\.ngrok-free\.(app|dev)|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from backend.src.utils.exceptions import ServiceError


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(status_code=400, content={"detail": exc.detail})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理：记录完整 traceback，返回统一 500"""
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误"},
    )

# 静态文件服务（图片生成等）
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
(static_dir / "images").mkdir(parents=True, exist_ok=True)
(static_dir / "avatars").mkdir(parents=True, exist_ok=True)
(static_dir / "ppt").mkdir(parents=True, exist_ok=True)
(static_dir / "presentations").mkdir(parents=True, exist_ok=True)
(static_dir / "videos").mkdir(parents=True, exist_ok=True)
(static_dir / "covers").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def hello():
    return {"hello": "user"}


@app.get("/debug/token/{user_id}")
async def debug_token(user_id: int):
    """调试用：输入用户 ID 直接返回 token（仅 DEBUG=true 时可用）"""
    import os
    if os.getenv("DEBUG", "").lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=404, detail="Not Found")
    from backend.src.utils.jwt import create_access_token
    return {"user_id": user_id, "token": create_access_token(user_id)}


@app.on_event("startup")
async def startup():
    await init_db()
    # 清理未完成的生成任务
    from backend.src.service.resource.service import ResourceService
    await ResourceService.init_tasks()
    # 预加载 BGE 模型，避免首次知识库操作时等待下载/加载
    from backend.src.utils.knowledge_base import _get_embed_model_async
    await _get_embed_model_async()
    # 启动定时任务（周报 + AI 建议）
    from backend.src.utils.scheduler import start
    start()
    # 初始化 Redis 连接 & 启动 SSE 跨进程转发器
    global _redis_forward_task
    try:
        from backend.src.utils.redis_client import get_redis, _forward_redis_messages
        await get_redis()
        _redis_forward_task = asyncio.create_task(_forward_redis_messages())
    except Exception:
        logger.warning("Redis 不可用，SSE 降级为单进程模式（不影响核心功能）")


@app.on_event("shutdown")
async def shutdown():
    from backend.src.utils.scheduler import stop
    stop()
    await close_db()
    # 关闭 Redis 连接 & 取消转发器
    global _redis_forward_task
    if _redis_forward_task and not _redis_forward_task.done():
        _redis_forward_task.cancel()
    from backend.src.utils.redis_client import close_redis
    await close_redis()

 
app.include_router(user_router)
app.include_router(chat_router)
app.include_router(portrait_router)
app.include_router(resource_router)
app.include_router(image_router)
app.include_router(knowledge_router)
app.include_router(admin_router)
app.include_router(exam_router)
app.include_router(path_router)
app.include_router(learning_path_router)
app.include_router(narration_router)
app.include_router(legacy_video_narration_router)
app.include_router(video_router)
app.include_router(study_router)
app.include_router(notification_router)
app.include_router(annotation_router)
