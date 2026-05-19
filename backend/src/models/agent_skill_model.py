from tortoise import Model, fields


class AgentSkill(Model):
    """用户个性化 skill — 每人每种资源类型最多一条"""
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=64, description="技能名称")
    resource_type = fields.CharField(max_length=32, description="资源类型: document/mindmap/exercise/case/reading")
    system_prompt = fields.TextField(description="用户定制的完整 system prompt")
    enabled = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    user = fields.ForeignKeyField(
        "models.User",
        related_name="agent_skills",
        on_delete=fields.CASCADE,
    )

    class Meta:
        table = "agent_skills"
        unique_together = [("user_id", "resource_type")]
