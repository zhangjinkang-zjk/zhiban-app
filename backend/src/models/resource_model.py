from tortoise import Model, fields


class GeneratedResource(Model):
    id = fields.IntField(pk=True, description="资源ID")
    topic = fields.CharField(max_length=255, description="学习主题")
    resource_type = fields.CharField(max_length=32, default="document", description="资源类型")
    content = fields.TextField(description="完整 Markdown 正文")
    review_passed = fields.BooleanField(default=False, description="是否通过审核")
    retry_count = fields.IntField(default=0, description="重试次数")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    user = fields.ForeignKeyField(
        "models.User",
        related_name="generated_resources",
        on_delete=fields.CASCADE,
    )

    class Meta:
        table = "generated_resources"
