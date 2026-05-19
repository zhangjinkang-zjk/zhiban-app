import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from backend.src.ai_core.llm_config import llm
from backend.src.utils.database import init_db
from backend.src.utils.portrait_utils import format_portrait
from backend.src.utils.prompt_loader import load_prompt
from backend.src.models.usermodel import User
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables import RunnableWithMessageHistory


@tool
async def get_used_history(user_id: str):
    """获取全部聊天记录，参数user_id为用户数字ID"""
    try:
        await init_db()
        user = await User.filter(id=int(user_id.strip())).first()
        if not user:
            return "未查找到该用户"
        history = await user.chat_history.all().order_by("created_at")
        if not history:
            return "该用户暂无聊天记录"
        chat_content = "【历史聊天记录】：\n"
        for message in history:
            chat_content += f"时间：{message.created_at} | 用户提问：{message.req} | AI回答：{message.res}\n"
        return chat_content
    except Exception as e:
        return f"获取历史记录失败：{e}"


@tool
async def get_character(user_id: str):
    """获取用户画像（只读），参数user_id为用户数字ID"""
    try:
        await init_db()
        user = await User.filter(id=int(user_id.strip())).first()
        if not user:
            return "未查找到该用户"
        picture = await user.picture
        if not picture:
            return "该用户暂无人设描述"
        return "\n".join(format_portrait(picture, show_missing=False))
    except Exception as e:
        return f"获取画像失败：{e}"


class DailyChat:
    def __init__(self, user_id: int, session_id: str = None):
        self.user_id = user_id
        self.session_id = session_id or f"session_{user_id}"
        self.store = {}
        self.agent_with_memory = self._build_agent()

    def _get_session_history(self, session_id: str):
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()
        return self.store[session_id]

    def _build_agent(self):
        system_prompt = load_prompt("chat/daily")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        tools = [get_used_history, get_character]
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
        has_token_stream = False

        async for event in self.agent_with_memory.astream_events(
            {"input": message, "current_user_id": str(self.user_id)},
            config={"configurable": {"session_id": self.session_id}},
            version="v2",
        ):
            if event.get("event") == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                content = getattr(chunk, "content", "")

                if content:
                    has_token_stream = True
                    yield content

            if event.get("event") == "on_chain_end" and not has_token_stream:
                output = event.get("data", {}).get("output", {})
                if isinstance(output, dict) and output.get("output"):
                    yield output["output"]
