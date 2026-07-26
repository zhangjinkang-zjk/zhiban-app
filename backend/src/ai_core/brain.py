import asyncio
import json
import logging
import re
import weakref
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from backend.src.ai_core.llm_config import llm
from backend.src.ai_core.tools.knowledge import (
    search_knowledge_base, ingest_document,
    list_knowledge, update_knowledge, delete_knowledge,
)
from backend.src.ai_core.tools.portrait import read_portrait, update_portrait
from backend.src.ai_core.tools.skill import (
    read_skill, upsert_skill, list_skills, delete_skill, create_action_skill,
)
from backend.src.ai_core.tools.resource import generate_learning_resource
from backend.src.ai_core.tools.search import web_search
from backend.src.ai_core.tools.image import generate_image
from backend.src.ai_core.tools.exam import generate_exam_questions
from backend.src.ai_core.tools.path import (
    list_learning_paths, get_learning_path_detail, enroll_learning_path,
    regenerate_learning_path, update_path_node, add_path_node, delete_path_node,
)
from backend.src.ai_core.tools.animation import generate_slide_animation
from backend.src.ai_core.tools.video_search import search_online_video
from backend.src.ai_core.tools.history import get_used_history
from backend.src.utils.prompt_loader import load_prompt
from pydantic import create_model, Field as PydanticField
try:
    from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
except ModuleNotFoundError:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool
from langchain_core.messages import HumanMessage, AIMessage


def _inject_user_id(tool, user_id: str):
    """拷贝一个 tool，移除 user_id 参数并自动注入当前用户 ID"""
    original_coro = tool.coroutine
    if tool.args_schema:
        fields = {}
        for name, field_info in tool.args_schema.model_fields.items():
            if name != "user_id":
                fields[name] = (field_info.annotation, field_info)
        new_schema = create_model(f"{tool.name}_input", **fields) if fields else None
    else:
        new_schema = None

    desc = (tool.description or "").replace("user_id用户数字ID", "")
    desc = desc.replace("，，", "，").replace("，。", "。").replace("参数：，", "参数：").strip()

    async def _scoped(**kwargs):
        kwargs["user_id"] = user_id
        return await original_coro(**kwargs)

    _scoped.__name__ = tool.name
    return StructuredTool.from_function(
        coroutine=_scoped,
        name=tool.name,
        description=desc,
        args_schema=new_schema,
    )


def _inject_chat_group_id(tool, chat_group_id: int):
    """为 get_used_history 注入当前聊天组 ID"""
    original_coro = tool.coroutine
    if tool.args_schema:
        fields = {}
        for name, field_info in tool.args_schema.model_fields.items():
            if name not in ("chat_group_id",):
                fields[name] = (field_info.annotation, field_info)
        new_schema = create_model(f"{tool.name}_scoped_input", **fields) if fields else None
    else:
        new_schema = None

    async def _scoped(**kwargs):
        kwargs["chat_group_id"] = chat_group_id
        return await original_coro(**kwargs)

    _scoped.__name__ = tool.name
    return StructuredTool.from_function(
        coroutine=_scoped,
        name=tool.name,
        description=(tool.description or ""),
        args_schema=new_schema,
    )


_MAX_HISTORY_TURNS = 20

<<<<<<< Updated upstream
=======
# ── 消息分类：按需加载工具行为指南 ──

_CREATE_TRIGGERS = [
    "生成学习", "生成资料", "生成文档", "做个PPT", "做PPT", "生成PPT",
    "做一份PPT", "做一个PPT", "弄个PPT", "弄一份PPT",
    "做课件", "生成课件", "弄个课件", "做个课件",
    "生成word", "生成Word", "做个word", "做个Word", "word文档", "Word文档",
    "整理成文档", "整理成PPT", "做成文档", "做成PPT",
    "生成思维导图", "生成脑图", "做思维导图", "做脑图",
    "帮我整理", "帮我总结", "帮我生成", "我要一份", "想要一份", "需要一份",
    "出题", "出几道", "出一些题", "出套题", "出一套题", "练习题", "测验", "考试模拟",
    "做几道题", "来几道题", "做题", "习题", "试卷", "题库", "考考我",
    "画一张", "画个", "生成图片", "生成一张图", "配图", "插图",
    "帮我画", "帮我生成图",
    "生成动画", "播放PPT", "演示PPT", "旁白", "念给我听",
    "生成视频", "生成学习视频", "做个视频", "学习视频",
    "搜视频", "找视频", "视频教程", "在线课程", "网课资源",
]

_CREATE_PATTERNS = [
    r"(?:帮我|给我|请|麻烦)?(?:生成|制作|做|写|整理|弄|搞)(?:一个|一份|一些|几道|一套|套|点)?(?:学习)?(?:资料|文档|word|Word|PPT|ppt|课件|思维导图|脑图|习题|题库|练习|试卷|测验|案例|阅读|图片|配图|插图|动画|旁白|学习视频|视频)",
    r"(?:我要|我想要|想要|需要|来个|来一份|给我来)(?:一个|一份|一些|几道|一套|套|点)?(?:学习)?(?:资料|文档|word|Word|PPT|ppt|课件|思维导图|脑图|习题|题库|练习|试卷|测验|案例|阅读|图片|配图|插图|学习视频)",
    r"(?:出|来|搞|弄)(?:几道|一些|一套|套)?(?:题|练习|习题|试卷)",
    r"(?:搜|找|搜索)(?:一下|一些|几个|个)?(?:教学)?(?:视频|视频教程|网课|课程资源)",
]

_MANAGE_TRIGGERS = [
    "学习路径", "课程路径", "学习计划", "选课", "有哪些路径",
    "加入路径", "路径管理", "修改节点", "添加节点", "删除节点",
    "重新规划路径", "路径不合适", "路径查看",
    "skill", "Skill", "自定义提示词", "修改提示词", "设置提示词",
    "恢复默认", "升级生成", "删除skill", "创建skill",
    "动作skill", "action skill", "添加能力", "添加工具",
]

_MANAGE_PATTERNS = [
    r"(?:查看|列出|加入|开始|选择|重建|重新规划|调整|修改|添加|删除).{0,12}(?:学习路径|课程路径|路径|节点|学习计划)",
    r"(?:升级|自定义|修改|设置|恢复|重置|删除).{0,12}(?:skill|Skill|提示词|生成风格|生成方式)",
    r"(?:添加|创建|接入).{0,12}(?:工具|能力|接口|API|api)",
]


def _classify_message(message: str) -> set[str]:
    """根据用户消息判断需要加载哪些工具行为指南"""
    text = str(message or "")
    cats = set()
    for t in _CREATE_TRIGGERS:
        if t in text:
            cats.add("create")
            break
    if "create" not in cats and any(re.search(pattern, text, re.IGNORECASE) for pattern in _CREATE_PATTERNS):
        cats.add("create")
    for t in _MANAGE_TRIGGERS:
        if t in text:
            cats.add("manage")
            break
    if "manage" not in cats and any(re.search(pattern, text, re.IGNORECASE) for pattern in _MANAGE_PATTERNS):
        cats.add("manage")
    return cats


>>>>>>> Stashed changes
class Brain:
    _instances: weakref.WeakSet = weakref.WeakSet()

    def __init__(self, user_id: int, chat_group_id: int | None = None, session_id: str | None = None):
        self.user_id = user_id
        self.chat_group_id = chat_group_id
        self.session_id = session_id or f"brain_{user_id}"
        self._raw_executor = None
        self._action_tools_loaded = False
        self._history: list = []
        Brain._instances.add(self)

    # ── 动态工具工厂 ──

    @staticmethod
    def _make_http_tool(skill: dict):
        """将 HTTP 类型的 action skill 包装成 LangChain StructuredTool"""
        config = json.loads(skill["action_config"]) if isinstance(skill["action_config"], str) else skill["action_config"]
        safe_name = skill["name"].replace("-", "_").replace(" ", "_")

        async def _handler(**kwargs):
            url = config["url"]
            for k, v in kwargs.items():
                url = url.replace(f"{{{{{k}}}}}", str(v))
            timeout = httpx.Timeout(30.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                method = config.get("method", "GET").upper()
                resp = await client.request(method, url)
                text = resp.text[:3000]
                if resp.status_code >= 400:
                    return f"请求失败 (HTTP {resp.status_code}): {text}"
                return text

        _handler.__name__ = safe_name

        params_schema = config.get("params", {})
        args_schema = None
        if params_schema:
            fields = {}
            for pname, pdesc in params_schema.items():
                if isinstance(pdesc, dict):
                    desc = str(pdesc.get("description") or pdesc.get("desc") or "")
                else:
                    desc = str(pdesc or "")
                fields[pname] = (str, PydanticField(description=desc))
            args_schema = create_model(f"{safe_name}_input", **fields)

        return StructuredTool.from_function(
            coroutine=_handler,
            name=safe_name,
            description=skill.get("tool_description", "") or f"自定义技能: {skill['name']}",
            args_schema=args_schema,
        )

    async def _load_action_tools_async(self):
        """在正确的 async 上下文中从 DB 加载 action skill"""
        from backend.src.service.skill import service as skill_service
        skills = await skill_service.list_actions(user_id=self.user_id)
        tools = []
        for s in skills:
            if s.get("action_type") != "http":
                continue
            try:
                tools.append(self._make_http_tool(s))
            except Exception:
                logging.getLogger(__name__).exception("action skill 构造失败，已跳过: %s", s.get("name"))
        return tools

    # ── 热刷新 ──

    @classmethod
    def rebuild_for_user(cls, user_id: int):
        """创建/删除 action skill 后标记需要刷新，下次对话时自动重建"""
        for inst in cls._instances:
            if inst.user_id == user_id:
                inst._action_tools_loaded = False

    def _build_agent(self, action_tools: list):
        system_prompt = load_prompt("chat/unified")
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        current_time_context = (
            "\n\n### 当前时间锚点\n"
            f"- 当前日期：{now.strftime('%Y-%m-%d')}\n"
            f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "- 时区：Asia/Shanghai\n"
            "- 进行搜索、新闻、时间、日程、时效性判断时，必须以这里的当前日期为准，不要沿用旧年份。\n"
            "- 如果用户提到“今天/当前/今年/最新/最近/2026年”等，先按这个时间锚点理解，再决定是否搜索，不要默认写成 2025 年。\n"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt + current_time_context),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        uid = str(self.user_id)
        gid = self.chat_group_id or 0
        tools = [
            _inject_user_id(search_knowledge_base, uid),
            _inject_user_id(ingest_document, uid),
            _inject_user_id(list_knowledge, uid),
            _inject_user_id(update_knowledge, uid),
            _inject_user_id(delete_knowledge, uid),
            _inject_user_id(read_portrait, uid),
            _inject_user_id(update_portrait, uid),
            _inject_chat_group_id(_inject_user_id(get_used_history, uid), gid),
            web_search,
            _inject_user_id(read_skill, uid),
            _inject_user_id(upsert_skill, uid),
            _inject_user_id(list_skills, uid),
            _inject_user_id(delete_skill, uid),
            _inject_user_id(create_action_skill, uid),
            _inject_chat_group_id(_inject_user_id(generate_learning_resource, uid), gid),
            _inject_chat_group_id(_inject_user_id(generate_image, uid), gid),
            _inject_chat_group_id(_inject_user_id(generate_exam_questions, uid), gid),
            _inject_chat_group_id(_inject_user_id(generate_slide_animation, uid), gid),
            _inject_chat_group_id(_inject_user_id(search_online_video, uid), gid),
            _inject_user_id(list_learning_paths, uid),
            _inject_user_id(get_learning_path_detail, uid),
            _inject_user_id(enroll_learning_path, uid),
            _inject_user_id(regenerate_learning_path, uid),
            _inject_user_id(update_path_node, uid),
            _inject_user_id(add_path_node, uid),
            _inject_user_id(delete_path_node, uid),
        ]
        tools.extend(_inject_user_id(t, uid) for t in action_tools)

        agent = create_tool_calling_agent(llm=llm, prompt=prompt, tools=tools)
        self._raw_executor = AgentExecutor(
            agent=agent, tools=tools,
            verbose=True, handle_parsing_errors=True, max_iterations=5,
        )

    async def _ensure_action_tools(self):
        """首次调用或 rebuild_for_user 后，异步加载 action tools 并重建 agent"""
        if self._action_tools_loaded:
            return
        try:
            action_tools = await self._load_action_tools_async()
        except Exception:
            logging.getLogger(__name__).exception("加载 action tools 失败")
            action_tools = []
        self._build_agent(action_tools)
        self._action_tools_loaded = True

    async def chat(self, message: str, resource_context: str = "", path_context: str = "", portrait_context: str = "") -> str:
        await self._ensure_action_tools()
        response = await self._raw_executor.ainvoke({
            "input": message,
            "history": list(self._history),
            "current_user_id": str(self.user_id),
            "resource_context": resource_context,
            "path_context": path_context,
            "portrait_context": portrait_context,
        })
        self._history.append(HumanMessage(content=message))
        self._history.append(AIMessage(content=response["output"]))
        if len(self._history) > _MAX_HISTORY_TURNS * 2:
            self._history = self._history[-_MAX_HISTORY_TURNS * 2:]
        return response["output"]

    async def stream(self, message: str, resource_context: str = "", path_context: str = "", portrait_context: str = ""):
        """逐 token 流式输出 — 包含工具调用事件，工具执行期间自动心跳保活"""
        await self._ensure_action_tools()

        full_response = ""
        tool_running = False

        async def _stream_events(version: str):
            nonlocal tool_running
            agen = self._raw_executor.astream_events(
                {
                    "input": message,
                    "history": list(self._history),
                    "current_user_id": str(self.user_id),
                    "resource_context": resource_context,
                    "path_context": path_context,
                    "portrait_context": portrait_context,
                },
                version=version,
            )
            while True:
                try:
                    event = await asyncio.wait_for(agen.__anext__(), timeout=30 if tool_running else 120)
                except asyncio.TimeoutError:
                    yield {"type": "keepalive"}
                    continue
                except StopAsyncIteration:
                    break
                yield event

        try:
            async for event in _stream_events("v2"):
                kind = event.get("event", "")

                if kind == "on_tool_start":
                    tool_running = True
                    tool_name = event.get("name", "")
                    yield {"role": "tool", "type": "tool_start", "tool": tool_name}

                elif kind == "on_tool_end":
                    tool_running = False
                    tool_name = event.get("name", "")
                    tool_output = event.get("data", {}).get("output", "")
                    if isinstance(tool_output, str) and len(tool_output) > 500:
                        tool_output = tool_output[:500] + "..."
                    yield {"role": "tool", "type": "tool_end", "tool": tool_name, "output": str(tool_output)}

                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk:
                        content = getattr(chunk, "content", None)
                        if content:
                            full_response += content
                            yield {"role": "assistant", "type": "chunk", "content": content}
        except (TypeError, NotImplementedError):
            async for event in _stream_events("v1"):
                kind = event.get("event", "")

                if kind == "on_tool_start":
                    tool_running = True
                    tool_name = event.get("name", "")
                    yield {"role": "tool", "type": "tool_start", "tool": tool_name}

                elif kind == "on_tool_end":
                    tool_running = False
                    tool_name = event.get("name", "")
                    tool_output = event.get("data", {}).get("output", "")
                    if isinstance(tool_output, str) and len(tool_output) > 500:
                        tool_output = tool_output[:500] + "..."
                    yield {"role": "tool", "type": "tool_end", "tool": tool_name, "output": str(tool_output)}

                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk:
                        content = getattr(chunk, "content", None)
                        if content:
                            full_response += content
                            yield {"role": "assistant", "type": "chunk", "content": content}

        self._history.append(HumanMessage(content=message))
        self._history.append(AIMessage(content=full_response))
        if len(self._history) > _MAX_HISTORY_TURNS * 2:
            self._history = self._history[-_MAX_HISTORY_TURNS * 2:]
