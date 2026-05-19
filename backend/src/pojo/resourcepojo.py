from pydantic import BaseModel, Field


class GenerateResourceRequest(BaseModel):
    topic: str = Field(description="学习主题，如 '线性代数矩阵运算'")
    resource_types: list[str] = Field(
        default=["document"],
        description="资源类型列表: document / ppt / mindmap / exercise / case / reading"
    )
