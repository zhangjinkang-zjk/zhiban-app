from pathlib import Path
from tortoise import Tortoise
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent.parent / ".env")

database = os.getenv("database")

#幂等初始化连接数据库，防止数据库重复连接
_DB_INITIALIZED = False

async def init_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED :
        return 
    await Tortoise.init(
        db_url=database,
        modules={"models": ["backend.src.models.usermodel", "backend.src.models.chat_history_model", "backend.src.models.portraitmodel", "backend.src.models.knowledgemodel", "backend.src.models.resource_model", "backend.src.models.agent_skill_model"]}
    )
    await Tortoise.generate_schemas()
    _DB_INITIALIZED = True

async def close_db():
    await Tortoise.close_connections()
