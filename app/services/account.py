"""账号相关业务：注册、邮箱验证、找回密码、改密。"""

from __future__ import annotations

import logging
import re

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import hash_password, password_problem, verify_password
from ..models import User
from . import auth as auth_service
from . import mailer, settings_service

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[\w一-鿿.-]{3,32}$", re.UNICODE)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class AccountError(Exception):
    def __init__(self, message: str, *, field: str = "form") -> None:
        self.message = message
        self.field = field
        super().__init__(message)


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AccountError("用户名需为 3-32 位中英文、数字、下划线或短横线", field="username")
    return username


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AccountError("邮箱格式不正确", field="email")
    return email


async def _verification_link(db: AsyncSession, request: Request | None, token: str) -> str:
    base = await settings_service.base_url(db, request)
    return f"{base}/verify?token={token}"


async def _reset_link(db: AsyncSession, request: Request | None, token: str) -> str:
    base = await settings_service.base_url(db, request)
    return f"{base}/reset?token={token}"


# ------------------------------------------------------------------ 注册


async def register(
    db: AsyncSession,
    *,
    username: str,
    email: str,
    password: str,
    request: Request | None = None,
) -> tuple[User, bool]:
    """创建账号并发送验证邮件。

    返回 ``(user, mail_sent)``：邮件发送失败不影响账号创建，用户可稍后重发。
    """
    username = validate_username(username)
    email = validate_email(email)

    problem = password_problem(password)
    if problem:
        raise AccountError(problem, field="password")

    if await auth_service.username_taken(db, username):
        raise AccountError("该用户名已被占用", field="username")
    if await auth_service.find_user_by_email(db, email):
        raise AccountError("该邮箱已被注册", field="email")

    require_verification = await settings_service.get_bool(
        db, "registration.require_email_verification", True
    )

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        email_verified=not require_verification,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    mail_sent = False
    if require_verification:
        mail_sent = await send_verification(db, user, request=request)
    return user, mail_sent


async def send_verification(
    db: AsyncSession, user: User, *, request: Request | None = None
) -> bool:
    """签发令牌并投递验证邮件；返回是否发送成功。"""
    token = await auth_service.issue_email_token(
        db, user, auth_service.PURPOSE_VERIFY, auth_service.VERIFY_TTL_MINUTES
    )
    link = await _verification_link(db, request, token)
    try:
        await mailer.send_verification_email(
            db,
            to=user.email,
            username=user.username,
            link=link,
            ttl_minutes=auth_service.VERIFY_TTL_MINUTES,
        )
        return True
    except mailer.MailError as exc:
        logger.warning("验证邮件发送失败 user=%s: %s", user.username, exc)
        return False


async def verify_email(db: AsyncSession, token: str) -> User | None:
    user = await auth_service.consume_email_token(db, token, auth_service.PURPOSE_VERIFY)
    if user is None:
        return None
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
    return user


# ------------------------------------------------------------------ 找回密码


async def request_password_reset(
    db: AsyncSession, email: str, *, request: Request | None = None
) -> bool:
    """无论邮箱是否存在都返回同样的结果，避免账号枚举。"""
    user = await auth_service.find_user_by_email(db, validate_email(email))
    if user is None or not user.is_active:
        return False

    token = await auth_service.issue_email_token(
        db, user, auth_service.PURPOSE_RESET, auth_service.RESET_TTL_MINUTES
    )
    link = await _reset_link(db, request, token)
    try:
        await mailer.send_password_reset_email(
            db,
            to=user.email,
            username=user.username,
            link=link,
            ttl_minutes=auth_service.RESET_TTL_MINUTES,
        )
    except mailer.MailError as exc:
        logger.warning("重置邮件发送失败 user=%s: %s", user.username, exc)
        raise AccountError(f"邮件发送失败：{exc}") from exc
    return True


async def reset_password(db: AsyncSession, token: str, new_password: str) -> User | None:
    problem = password_problem(new_password)
    if problem:
        raise AccountError(problem, field="password")

    user = await auth_service.consume_email_token(db, token, auth_service.PURPOSE_RESET)
    if user is None:
        return None

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await db.commit()
    # 改密后所有旧会话失效
    await auth_service.revoke_all_sessions(db, user.id)
    return user


# ------------------------------------------------------------------ 修改密码


async def change_password(
    db: AsyncSession, user: User, *, current: str, new: str, keep_session_token: str | None = None
) -> None:
    if not verify_password(current, user.password_hash):
        raise AccountError("当前密码不正确", field="current")
    problem = password_problem(new)
    if problem:
        raise AccountError(problem, field="password")
    if current == new:
        raise AccountError("新密码不能与当前密码相同", field="password")

    user.password_hash = hash_password(new)
    user.must_change_password = False
    await db.commit()

    # 其它设备全部下线，保留当前设备
    await auth_service.revoke_all_sessions(db, user.id, except_token=keep_session_token)
