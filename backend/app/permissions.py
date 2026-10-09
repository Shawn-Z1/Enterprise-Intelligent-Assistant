from app.ids import ShortId

from pydantic import BaseModel, ConfigDict


class AuthContext(BaseModel):
    """每次由当前 PG 身份生成的可信上下文，绝不由客户端角色或职级字段构建。"""

    model_config = ConfigDict(frozen=True)
    user_id: ShortId
    username: str
    display_name: str
    role: str
    department_id: ShortId | None
    department_name: str | None
    grade: int | None
    employee_id: ShortId | None
    position: str | None
    knowledge_enabled: bool
    business_query_enabled: bool
    permission_version: int
    session_id: ShortId
    csrf_token_hash: str

    @property
    def is_admin(self) -> bool:
        """无参数，根据服务端可信角色返回是否管理员，不依赖职级。"""
        return self.role == "admin"

    @property
    def can_use_knowledge(self) -> bool:
        """无参数，按当前角色/部门/职级和授权返回知识使用资格。"""
        return self.is_admin or bool(self.knowledge_enabled and self.department_id and self.grade)

    @property
    def can_query_business(self) -> bool:
        """无参数，检查业务开关及可信身份资格；不表示拥有全部业务数据。"""
        return self.business_query_enabled and (self.is_admin or bool(self.department_id and self.grade))

    def public_profile(self) -> dict:
        """无参数，返回前端可见本人资料；排除会话和 CSRF 哈希及所有凭证。"""
        data = self.model_dump(mode="json", exclude={"session_id", "csrf_token_hash"})
        data.update(can_use_knowledge=self.can_use_knowledge, can_query_business=self.can_query_business)
        return data


def can_read_knowledge(context: AuthContext, status: str, allowed_departments: list[ShortId], minimum_grade: int | None) -> bool:
    """接收可信身份、文档状态与 ACL；返回正式知识读取资格，不给创建者额外特权。"""
    if status != "retrievable" or not context.can_use_knowledge:
        return False
    return context.is_admin or ((not allowed_departments or context.department_id in allowed_departments)
                               and (minimum_grade is None or (context.grade or 0) >= minimum_grade))
