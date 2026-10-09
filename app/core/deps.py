"""FastAPI 依赖：认证上下文、CSRF 校验、模板上下文。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User, UserSession
from ..services import auth as auth_service
from ..services import settings_service
from .config import settings
from .database import get_db

CSRF_COOKIE = "ft_csrf"
CSRF_HEADER = "x-csrf-token"


@dataclass(slots=True)
class AuthContext:
    user: User
    session: UserSession

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


class LoginRequired(Exception):
    """页面级依赖：未登录时跳转登录页，而不是返回 401 JSON。"""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url
        super().__init__("login required")


class PasswordChangeRequired(Exception):
    """默认口令未修改时，强制先跳转到个人设置页。"""


class AdminRequired(Exception):
    def __init__(self, message: str = "需要管理员权限") -> None:
        self.message = message
        super().__init__(message)


async def get_auth(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext | None:
    """解析当前登录态；未登录返回 None。"""
    raw = request.cookies.get(settings.session_cookie_name)
    resolved = await auth_service.resolve_session(db, raw)
    if resolved is None:
        return None
    user, row = resolved
    await auth_service.touch_session(db, row)
    return AuthContext(user=user, session=row)


async def require_user(auth: AuthContext | None = Depends(get_auth)) -> AuthContext:
    """API 用：未登录返回 401。"""
    if auth is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return auth


async def require_admin(auth: AuthContext | None = Depends(get_auth)) -> AuthContext:
    """API 用：需要管理员。"""
    if auth is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return auth


async def require_user_page(
    request: Request, auth: AuthContext | None = Depends(get_auth)
) -> AuthContext:
    """页面用：未登录跳转登录页并带回跳地址。"""
    if auth is None:
        raise LoginRequired(next_url=request.url.path)
    # 默认管理员口令未修改前，除个人设置外一律先跳过去改密
    if auth.user.must_change_password and not request.url.path.startswith("/me/settings"):
        raise PasswordChangeRequired()
    return auth


async def require_admin_page(
    request: Request, auth: AuthContext | None = Depends(get_auth)
) -> AuthContext:
    if auth is None:
        raise LoginRequired(next_url=request.url.path)
    if not auth.user.is_admin:
        raise AdminRequired()
    return auth


def check_csrf(request: Request) -> None:
    """所有写操作都必须带上与 Cookie 一致的 ``X-CSRF-Token``。

    采用双提交 Cookie 模式：攻击者站点无法读取本站 Cookie，因此无法伪造该请求头。
    """
    expected = getattr(request.state, "csrf_token", None) or request.cookies.get(CSRF_COOKIE)
    provided = request.headers.get(CSRF_HEADER) or ""
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 校验失败，请刷新页面后重试")


async def page_context(
    request: Request,
    db: AsyncSession,
    auth: AuthContext | None = None,
    **extra: object,
) -> dict[str, object]:
    """页面渲染的公共上下文：站点设置 + 登录态 + CSRF。

    ``cfg`` 是模板里可直接点号访问的扁平配置，例如 ``{{ cfg.name }}``。
    """
    context: dict[str, object] = {
        "request": request,
        "cfg": await settings_service.template_settings(db),
        "current_user": auth.user if auth else None,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "auth": auth,
    }
    context.update(extra)
    return context
