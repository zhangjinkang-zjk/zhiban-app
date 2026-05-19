"""
LangGraph 多智能体编排 — 学习资源生成
LeaderAgent → [ExecutorAgent × N 多线程并行] → ReviewerAgent
"""
import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

sys.path.append(str(Path(__file__).parent.parent))

from backend.src.ai_core.llm_config import llm
from backend.src.utils.prompt_loader import load_prompt

# 资源类型 → 默认 prompt 路径
PROMPT_MAP = {
    "document": "resource/executor",
    "ppt": "resource/executor_ppt",
    "mindmap": "resource/executor",
    "exercise": "resource/executor",
    "case": "resource/executor",
    "reading": "resource/executor",
}


def _fill(template: str, **kwargs) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


# ═══════════════════════════════════════
#  State
# ═══════════════════════════════════════

class ResourceState(TypedDict):
    user_id: str
    topic: str
    resource_types: list[str]           # ["document", "ppt"]
    portrait_context: str
    kb_context: str
    custom_prompts: dict                # {"document": "用户定制prompt", ...}
    generated_resources: dict           # {"document": "内容", "ppt": "内容"}
    review_feedback: str
    review_passed: bool
    retry_count: int


# ═══════════════════════════════════════
#  Nodes
# ═══════════════════════════════════════

async def leader_node(state: ResourceState) -> dict:
    """LeaderAgent: 分析需求，决定生成哪些资源类型"""
    topic = state["topic"]
    portrait = state.get("portrait_context", "")
    kb = state.get("kb_context", "")
    prompt_text = _fill(load_prompt("resource/leader"), topic=topic, portrait_context=portrait, kb_context=kb)

    response = await llm.ainvoke(prompt_text)

    try:
        content = response.content.strip()
        # 去掉可能的 markdown 代码块包裹
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content[:-3]
        plan = json.loads(content)
    except json.JSONDecodeError:
        plan = {"resource_types": ["document"], "topic": topic, "outline": content}

    # 用户已指定资源类型则尊重用户选择，否则由 Leader 决定
    requested = state.get("resource_types", ["document"])
    if requested != ["document"]:
        resource_types = requested
    else:
        resource_types = plan.get("resource_types", ["document"])

    return {"resource_types": resource_types}


async def executor_node(state: ResourceState) -> dict:
    """多 Executor 多线程并行生成 — ThreadPoolExecutor"""
    topic = state["topic"]
    resource_types = state.get("resource_types", ["document"])
    portrait = state.get("portrait_context", "")
    kb = state.get("kb_context", "")
    custom_prompts = state.get("custom_prompts", {}) or {}
    feedback = state.get("review_feedback", "")

    # 预先构建每个类型的 prompt（同步，快）
    prompts: dict[str, str] = {}
    for rt in resource_types:
        custom = custom_prompts.get(rt, "")
        if custom.strip():
            template = custom
        else:
            prompt_path = PROMPT_MAP.get(rt, "resource/executor")
            template = load_prompt(prompt_path)
        prompts[rt] = _fill(
            template,
            topic=topic,
            resource_type=rt,
            portrait_context=portrait,
            kb_context=kb,
            feedback=feedback,
        )

    # 多线程并行调 LLM（同步 invoke）
    def gen_one_sync(rt: str) -> tuple[str, str]:
        response = llm.invoke(prompts[rt])
        return rt, response.content

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=len(resource_types)) as pool:
        tasks = [loop.run_in_executor(pool, gen_one_sync, rt) for rt in resource_types]
        results = await asyncio.gather(*tasks)

    retry = state.get("retry_count", 0)
    if feedback:
        retry += 1

    return {
        "generated_resources": dict(results),
        "retry_count": retry,
        "review_feedback": "",
    }


async def reviewer_node(state: ResourceState) -> dict:
    """ReviewerAgent: 审核所有生成内容"""
    generated = state.get("generated_resources", {})

    parts = []
    for rt, content in generated.items():
        parts.append(f"## [{rt}]\n{content[:2000]}...")
    combined = "\n\n".join(parts)

    prompt_text = _fill(load_prompt("resource/reviewer"), content=combined)

    response = await llm.ainvoke(prompt_text)

    try:
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content[:-3]
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"passed": True, "score": 70, "feedback": response.content}

    passed = result.get("passed", False)
    if isinstance(passed, str):
        passed = passed.lower() == "true"

    return {
        "review_passed": bool(passed),
        "review_feedback": result.get("feedback", ""),
    }


# ═══════════════════════════════════════
#  Router
# ═══════════════════════════════════════

def should_continue(state: ResourceState) -> str:
    if state.get("review_passed"):
        return "end"
    if state.get("retry_count", 0) >= 2:
        return "end"
    return "executor"


# ═══════════════════════════════════════
#  Graph
# ═══════════════════════════════════════

def build_graph() -> StateGraph:
    workflow = StateGraph(ResourceState)

    workflow.add_node("leader", leader_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("reviewer", reviewer_node)

    workflow.add_edge(START, "leader")
    workflow.add_edge("leader", "executor")
    workflow.add_edge("executor", "reviewer")
    workflow.add_conditional_edges(
        "reviewer",
        should_continue,
        {"executor": "executor", "end": END},
    )

    return workflow.compile()


resource_graph = build_graph()
