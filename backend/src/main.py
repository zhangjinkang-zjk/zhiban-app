from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.src.utils.database import init_db, close_db
from backend.src.router.agentsrouter.unified_chat_router import router as unified_chat_router
from backend.src.router.agentsrouter.portrait_router import router as portrait_router
from backend.src.router.agentsrouter.resource_router import router as resource_router
from backend.src.router.knowledge_router import router as knowledge_router
from backend.src.router.userrouter import router as user_router
from backend.src.router.admin_router import router as admin_router
from backend.src.utils.jwt import create_access_token

app = FastAPI(
    title="AI聊天后端",
    description="Swagger接口文档",
    swagger_ui_init_oauth={},
)

# 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，生产环境建议指定具体域名
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有请求头
)


@app.get("/")
async def hello():
    return {"hello": "user"}


@app.post("/get_token")
async def get_token(user_id: int):
    return {"token": create_access_token(user_id)}


@app.on_event("startup")
async def startup():
    await init_db()


@app.on_event("shutdown")
async def shutdown():
    await close_db()


app.include_router(user_router)
app.include_router(unified_chat_router)
app.include_router(portrait_router)
app.include_router(resource_router)
app.include_router(knowledge_router)
app.include_router(admin_router)
