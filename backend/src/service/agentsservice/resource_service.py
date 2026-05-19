import json

from backend.src.ai_core.graph import resource_graph
from backend.src.models.resource_model import GeneratedResource
from backend.src.models.agent_skill_model import AgentSkill
from backend.src.models.usermodel import User
from backend.src.utils.database import init_db
from backend.src.utils.portrait_utils import format_portrait
from backend.src.utils.knowledge_base import search as kb_search


async def _make_state(topic: str, user_id: int, resource_types: list[str]) -> dict:
    await init_db()

    portrait_context = "暂无画像数据"
    user = await User.filter(id=user_id).first()
    if user:
        picture = await user.picture
        if picture:
            portrait_context = "\n".join(format_portrait(picture, show_missing=False))

    kb_context = "暂无相关知识库资料"
    try:
        kb_result = await kb_search(topic, top_k=5, user_id=user_id)
        if kb_result and "暂无" not in kb_result:
            kb_context = kb_result
    except Exception:
        pass

    custom_prompts = {}
    skills = await AgentSkill.filter(user_id=user_id, enabled=True).all()
    for s in skills:
        if s.resource_type in resource_types and s.system_prompt:
            custom_prompts[s.resource_type] = s.system_prompt

    return {
        "user_id": str(user_id),
        "topic": topic,
        "resource_types": resource_types,
        "portrait_context": portrait_context,
        "kb_context": kb_context,
        "custom_prompts": custom_prompts,
        "generated_resources": {},
        "review_feedback": "",
        "review_passed": False,
        "retry_count": 0,
    }


async def _save_resources(topic: str, user_id: int, generated: dict, review_passed: bool, retry_count: int) -> list[dict]:
    """存库并返回记录列表"""
    user = await User.filter(id=user_id).first()
    if not user:
        return []
    saved = []
    for rt, content in generated.items():
        record = await GeneratedResource.create(
            topic=topic,
            resource_type=rt,
            content=content,
            review_passed=review_passed,
            retry_count=retry_count,
            user=user,
        )
        saved.append({
            "resource_id": record.id,
            "topic": record.topic,
            "resource_type": record.resource_type,
            "content": record.content,
            "review_passed": record.review_passed,
            "retry_count": record.retry_count,
        })
    return saved


class ResourceService:

    @staticmethod
    async def generate_and_save(topic: str, user_id: int, resource_types: list[str]) -> list[dict]:
        initial_state = await _make_state(topic, user_id, resource_types)
        result = await resource_graph.ainvoke(initial_state)
        return await _save_resources(
            topic, user_id,
            result.get("generated_resources", {}),
            result.get("review_passed", False),
            result.get("retry_count", 0),
        )

    @staticmethod
    async def generate_stream(topic: str, user_id: int, resource_types: list[str]):
        """节点级流式 — astream 逐节点产出状态，只跑一次 graph"""
        initial_state = await _make_state(topic, user_id, resource_types)
        final_resources = {}
        final_passed = False
        final_retry = 0

        async for chunk in resource_graph.astream(initial_state, stream_mode="values"):
            # chunk 是当前累积的完整 state
            resources = chunk.get("generated_resources", {})
            if resources:
                final_resources = resources
            final_passed = chunk.get("review_passed", False)
            final_retry = chunk.get("retry_count", 0)

            yield f"data: {json.dumps({'resources': list(resources.keys()), 'review_passed': final_passed}, ensure_ascii=False)}\n\n"

        # 流式结束后存库
        saved = await _save_resources(topic, user_id, final_resources, final_passed, final_retry)
        yield f"data: {json.dumps({'done': True, 'resources': saved}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    @staticmethod
    async def get_resource(resource_id: int, user_id: int) -> dict | None:
        record = await GeneratedResource.filter(id=resource_id, user_id=user_id).first()
        if not record:
            return None
        return {
            "resource_id": record.id,
            "topic": record.topic,
            "resource_type": record.resource_type,
            "content": record.content,
            "review_passed": record.review_passed,
            "retry_count": record.retry_count,
            "created_at": str(record.created_at),
        }

    @staticmethod
    async def list_resources(user_id: int) -> list[dict]:
        records = await GeneratedResource.filter(user_id=user_id).order_by("-created_at").all()
        return [
            {
                "resource_id": r.id,
                "topic": r.topic,
                "resource_type": r.resource_type,
                "review_passed": r.review_passed,
                "created_at": str(r.created_at),
            }
            for r in records
        ]

    @staticmethod
    async def download_resource(resource_id: int, user_id: int) -> tuple[str, str, str] | None:
        record = await GeneratedResource.filter(id=resource_id, user_id=user_id).first()
        if not record:
            return None
        ext = "md"
        filename = f"{record.topic}_{record.resource_type}.{ext}"
        return record.content, filename, "text/markdown; charset=utf-8"

    @staticmethod
    async def delete_resource(resource_id: int, user_id: int) -> bool:
        record = await GeneratedResource.filter(id=resource_id, user_id=user_id).first()
        if not record:
            return False
        await record.delete()
        return True
