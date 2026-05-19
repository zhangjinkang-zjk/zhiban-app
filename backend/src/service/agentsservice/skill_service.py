from backend.src.models.agent_skill_model import AgentSkill

ALLOWED_TYPES = {"document", "ppt", "mindmap", "exercise", "case", "reading"}


class SkillService:

    @staticmethod
    async def upsert(user_id: int, resource_type: str, name: str, system_prompt: str) -> dict:
        if resource_type not in ALLOWED_TYPES:
            raise ValueError(f"不支持的资源类型: {resource_type}，可选: {', '.join(sorted(ALLOWED_TYPES))}")

        skill = await AgentSkill.filter(user_id=user_id, resource_type=resource_type).first()

        if skill:
            skill.name = name
            skill.system_prompt = system_prompt
            skill.enabled = True
            await skill.save()
            action = "updated"
        else:
            skill = await AgentSkill.create(
                user_id=user_id,
                resource_type=resource_type,
                name=name,
                system_prompt=system_prompt,
            )
            action = "created"

        return {
            "skill_id": skill.id,
            "resource_type": skill.resource_type,
            "name": skill.name,
            "action": action,
        }

    @staticmethod
    async def get(user_id: int, resource_type: str) -> dict | None:
        skill = await AgentSkill.filter(user_id=user_id, resource_type=resource_type, enabled=True).first()
        if not skill:
            return None
        return {
            "skill_id": skill.id,
            "resource_type": skill.resource_type,
            "name": skill.name,
            "system_prompt": skill.system_prompt,
            "created_at": str(skill.created_at),
            "updated_at": str(skill.updated_at),
        }

    @staticmethod
    async def list_all(user_id: int) -> list[dict]:
        skills = await AgentSkill.filter(user_id=user_id, enabled=True).all()
        return [
            {
                "skill_id": s.id,
                "resource_type": s.resource_type,
                "name": s.name,
                "prompt_length": len(s.system_prompt),
                "updated_at": str(s.updated_at),
            }
            for s in skills
        ]

    @staticmethod
    async def delete(user_id: int, resource_type: str) -> str:
        skill = await AgentSkill.filter(user_id=user_id, resource_type=resource_type).first()
        if not skill:
            return "技能不存在"
        await skill.delete()
        return f"技能 '{resource_type}' 已删除，恢复默认 prompt"
