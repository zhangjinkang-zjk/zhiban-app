from pydantic import BaseModel, Field


class UpsertSkillRequest(BaseModel):
    resource_type: str = Field(description="资源类型: document/mindmap/exercise/case/reading")
    name: str = Field(description="技能名称，如'我的文档风格'")
    system_prompt: str = Field(description="完整的 system prompt 模板，可含 {topic}{portrait_context}{kb_context}{feedback} 占位符")
