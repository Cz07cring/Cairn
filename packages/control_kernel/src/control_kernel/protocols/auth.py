"""浏览器 Session 协议（05§5 auth）。"""

from datetime import datetime

from pydantic import Field

from .projects import Contract


class AuthSession(Contract):
    user_id: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(min_length=1, max_length=32)
    project_ids: list[str] = Field(max_length=10000)
    csrf_token: str = Field(min_length=16, max_length=200)
    expires_at: datetime


class LogoutResult(Contract):
    logged_out: bool
