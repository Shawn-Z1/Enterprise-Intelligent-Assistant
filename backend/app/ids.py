import secrets
from typing import Annotated

from pydantic import StringConstraints


# 所有实体和追踪编号使用短字符串；认证令牌仍由认证模块独立生成。
ShortId = Annotated[str, StringConstraints(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")]


def new_id() -> str:
    """无参数，生成包含 96 位随机信息的 16 字符编号；返回 URL 安全字符串，不用作登录凭证。"""
    return secrets.token_urlsafe(12)
