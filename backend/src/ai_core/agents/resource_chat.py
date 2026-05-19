import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from backend.src.ai_core.llm_config import llm
from backend.src.utils.prompt_loader import load_prompt
from backend.src.utils.knowledge_base import (
    search as kb_search,
    ingest as kb_ingest,
    list_all as kb_list,
    update as kb_update,
    delete as kb_delete,
)

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables import RunnableWithMessageHistory


# ── RAG 工具 ──

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


# ── ResourceChat Agent ──

class ResourceChat:
    def __init__(self, user_id: int, session_id: str = None):
        self.user_id = user_id
        self.session_id = session_id or f"resource_{user_id}"
        self.store = {}
        self.agent_with_memory = self._build_agent()

    def _get_session_history(self, session_id: str):
        if session_id not in self.store:
            self.store[session_id] = ChatMessageHistory()
        return self.store[session_id]

    def _build_agent(self):
        system_prompt = load_prompt("resource/knowledge")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        tools = [search_knowledge_base, ingest_document, list_knowledge, update_knowledge, delete_knowledge]
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
