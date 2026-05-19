from backend.src.models.usermodel import User
from backend.src.utils.knowledge_base import list_grouped
from backend.src.utils.pwintohash import get_password_hash


class AdminService:

    @staticmethod
    async def list_users() -> list[dict]:
        users = await User.all().prefetch_related("picture")
        return [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "university": u.university,
                "grade": u.grade,
                "major": u.major,
                "email": u.email,
                "phonenum": u.phonenum,
                "profile": u.profile,
                "has_picture": u.picture_id is not None,
                "created_at": str(u.created_at),
            }
            for u in users
        ]

    @staticmethod
    async def delete_user(user_id: int) -> str:
        user = await User.filter(id=user_id).first()
        if not user:
            return "用户不存在"
        if user.role == "admin":
            return "不能删除管理员"
        username = user.username
        await user.delete()
        return f"用户 '{username}' 已删除"

    @staticmethod
    async def reset_password(user_id: int, new_password: str) -> str:
        user = await User.filter(id=user_id).first()
        if not user:
            return "用户不存在"
        user.password = get_password_hash(new_password)
        await user.save()
        return f"用户 '{user.username}' 密码已重置"

    @staticmethod
    async def list_knowledge_base() -> list[dict]:
        return await list_grouped()
