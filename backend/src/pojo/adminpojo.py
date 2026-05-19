from pydantic import BaseModel, Field


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, description="新密码")


class DeleteUserRequest(BaseModel):
    confirm: bool = Field(description="确认删除，必须为 true")
