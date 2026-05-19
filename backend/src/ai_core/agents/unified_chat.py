import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from backend.src.ai_core.llm_config import llm
from backend.src.utils.database import init_db
from backend.src.utils.prompt_loader import load_prompt
from backend.src.utils.knowledge_base import (
    search as kb_search,
    ingest as kb_ingest,
    list_all as kb_list,
    update as kb_update,
    delete as kb_delete,
)
from backend.src.utils.portrait_utils import (
    TRAIT_KEYS, format_portrait,
    parse_traits, dump_traits, build_trait_entry,
)
from backend.src.models.usermodel import User
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables import RunnableWithMessageHistory


# ═══════════════════════════════════════
#  工具：知识库
# ═══════════════════════════════════════

@tool
async def search_knowledge_base(query: str, user_id: str, top_k: int = 5):
    """
    从知识库中检索相关资料。
    - query: 用户想查询的问题或关键词
    - user_id: 当前用户的数字 ID（必须传入）
    - top_k: 返回最相关的几条资料，默认5条
    """
    return await kb_search(query, top_k, user_id=int(user_id))


@tool
async def ingest_document(title: str, content: str, user_id: str):
    """
    向知识库添加一篇新资料。
    - title: 资料标题
    - content: 资料正文
    - user_id: 当前用户的数字 ID（必须传入）
    """
    return await kb_ingest(title, content, user_id=int(user_id))


@tool
async def list_knowledge(user_id: str):
    """
    列出知识库中该用户可见的全部资料（公开资料 + 自己的私有资料）。
    - user_id: 当前用户的数字 ID（必须传入）
    """
    records = await kb_list(user_id=int(user_id))
    if not records:
        return "知识库中暂无资料"
    lines = ["知识库资料列表："]
    for i, r in enumerate(records, 1):
        label = "公开" if r["visibility"] == "public" else "私有"
        lines.append(f"{i}. [{label}] {r['title']} (id: {r['doc_id']})")
        lines.append(f"   内容摘要：{r['content'][:120]}...")
    return "\n".join(lines)


@tool
async def update_knowledge(doc_id: str, user_id: str, title: str = None, content: str = None):
    """
    更新知识库中的一条资料。
    - doc_id: 要更新的资料 ID
    - user_id: 当前用户的数字 ID（必须传入）
    - title: 新标题（可选，不传则不修改）
    - content: 新内容（可选，不传则不修改）
    """
    return await kb_update(
        doc_id=doc_id,
        title=title,
        content=content,
        user_id=int(user_id),
        is_admin=False,
    )


@tool
async def delete_knowledge(doc_id: str, user_id: str):
    """
    删除知识库中的一条资料（仅限自己上传的私有资料）。
    - doc_id: 要删除的资料 ID
    - user_id: 当前用户的数字 ID（必须传入）
    """
    return await kb_delete(doc_id=doc_id, user_id=int(user_id), is_admin=False)


# ═══════════════════════════════════════
#  工具：画像
# ═══════════════════════════════════════

@tool
async def read_portrait(user_id: str, show_missing: bool = False):
    """获取用户完整画像。参数：user_id用户数字ID，show_missing是否显示待补充维度（默认False）"""
    try:
        await init_db()
        user = await User.filter(id=int(user_id.strip())).first()
        if not user:
            return "未查找到该用户"
        picture = await user.picture
        if not picture:
            return "该用户尚未创建画像"
        return "\n".join(format_portrait(picture, show_missing=show_missing))
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


# ═══════════════════════════════════════
#  工具：历史记录
# ═══════════════════════════════════════

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


# ═══════════════════════════════════════
#  UnifiedChat Agent
# ═══════════════════════════════════════

class UnifiedChat:
    def __init__(self, user_id: int, session_id: str = None):
        self.user_id = user_id
        self.session_id = session_id or f"unified_{user_id}"
        self.store = {}
        self._raw_executor = None  # AgentExecutor 裸实例（不含 message history 包装）
        self.agent_with_memory = self._build_agent()

    def _get_session_history(self, session_id: str):
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()
        return self.store[session_id]

    def _build_agent(self):
        system_prompt = load_prompt("chat/unified")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        tools = [
            search_knowledge_base, ingest_document,
            list_knowledge, update_knowledge, delete_knowledge,
            read_portrait, update_portrait,
            get_used_history,
        ]
        agent = create_tool_calling_agent(llm=llm, prompt=prompt, tools=tools)
        agent_executor = AgentExecutor(
            agent=agent, tools=tools,
            verbose=True, handle_parsing_errors=True, max_iterations=5,
        )
        # 保留裸 AgentExecutor（流式时手动传 history）
        self._raw_executor = agent_executor
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
        """逐 token 流式输出 — 用 astream_events 从 LLM 层捕获 token，手动维护历史"""
        history = self._get_session_history(self.session_id)
        history_messages = list(history.messages)  # 历史对话（不含当前这一轮）

        full_response = ""

        def _is_stream_content(event: dict) -> str | None:
            """提取 LLM 流式 chunk 的文本内容"""
            if event.get("event") != "on_chat_model_stream":
                return None
            chunk = event.get("data", {}).get("chunk")
            if chunk is None:
                return None
            content = getattr(chunk, "content", None)
            return content if content else None

        try:
            async for event in self._raw_executor.astream_events(
                {
                    "input": message,
                    "history": history_messages,
                    "current_user_id": str(self.user_id),
                },
                version="v2",
            ):
                content = _is_stream_content(event)
                if content:
                    full_response += content
                    yield content
        except (TypeError, NotImplementedError):
            # langchain 版本较旧，降级用 v1
            async for event in self._raw_executor.astream_events(
                {
                    "input": message,
                    "history": history_messages,
                    "current_user_id": str(self.user_id),
                },
                version="v1",
            ):
                content = _is_stream_content(event)
                if content:
                    full_response += content
                    yield content

        # 流式结束后手动更新历史记录
        history.add_user_message(message)
        history.add_ai_message(full_response)
