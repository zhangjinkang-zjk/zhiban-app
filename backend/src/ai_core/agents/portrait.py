import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from backend.src.ai_core.llm_config import llm
from backend.src.utils.database import init_db
from backend.src.utils.portrait_utils import (
    TRAIT_KEYS,
    parse_traits, dump_traits, build_trait_entry, format_portrait,
)
from backend.src.utils.prompt_loader import load_prompt
from backend.src.models.usermodel import User
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables import RunnableWithMessageHistory


@tool
async def get_portrait(user_id: str):
    """获取用户完整画像，含各维度置信度。参数user_id为用户数字ID"""
    try:
        await init_db()
        user = await User.filter(id=int(user_id.strip())).first()
        if not user:
            return "未查找到该用户"
        picture = await user.picture
        if not picture:
            return "该用户尚未创建画像"
        return "\n".join(format_portrait(picture, show_missing=True))
    except Exception as e:
        return f"获取画像失败：{e}"


@tool
async def update_portrait(user_id: str, field: str, value: str, source: str = "user_stated"):
    """更新用户画像的指定维度，自动计算置信度。参数: user_id用户数字ID, field维度名, value维度值, source来源类型"""
    try:
        await init_db()
        user_id_int = int(user_id.strip())

        if field not in TRAIT_KEYS:
            return f"未知维度 '{field}'，可选：{', '.join(TRAIT_KEYS)}"

        user = await User.filter(id=user_id_int).first()
        if not user:
            return "未查找到该用户"

        picture = await user.picture
        if not picture:
            return "该用户尚未创建画像"

        traits = parse_traits(picture.traits)
        existing = traits.get(field) if isinstance(traits.get(field), dict) else None
        traits[field] = build_trait_entry(value, source, existing)
        picture.traits = dump_traits(traits)
        await picture.save()

        entry = traits[field]
        return (
            f"维度 '{field}' 已更新：{value}，"
            f"置信度 {entry['confidence']}（来源：{source}）"
        )
    except Exception as e:
        return f"更新画像失败：{e}"


class PortraitChat:
    def __init__(self, user_id: int, session_id: str = None):
        self.user_id = user_id
        self.session_id = session_id or f"portrait_{user_id}"
        self.store = {}
        self.agent_with_memory = self._build_agent()

    def _get_session_history(self, session_id: str):
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()
        return self.store[session_id]

    def _build_agent(self):
        system_prompt = load_prompt("portrait/portrait")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        tools = [get_portrait, update_portrait]
        agent = create_tool_calling_agent(llm=llm, prompt=prompt, tools=tools)
        agent_executor = AgentExecutor(
            agent=agent, tools=tools,
            verbose=True, handle_parsing_errors=True, max_iterations=5,
        )
        return RunnableWithMessageHistory(
            runnable=agent_executor,
            get_session_history=self._get_session_history,
            input_messages_key="input",
            history_messages_key="history",
        )

    async def chat(self, message: str) -> str:
        response = await self.agent_with_memory.ainvoke(
            {"input": message, "current_user_id": str(self.user_id)},
            config={"configurable": {"session_id": self.session_id}},
        )
        return response["output"]

    async def stream(self, message: str):
        """流式生成回答，逐块产出文本"""
        async for chunk in self.agent_with_memory.astream(
            {"input": message, "current_user_id": str(self.user_id)},
            config={"configurable": {"session_id": self.session_id}},
        ):
            if isinstance(chunk, dict) and "output" in chunk:
                yield chunk["output"]
