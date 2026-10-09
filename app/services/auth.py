"""会话与邮箱令牌的签发 / 校验。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.security import new_csrf_token, new_token, token_hash
from ..models import EmailToken, User, UserSession, utcnow

VERIFY_TTL_MINUTES = 30
RESET_TTL_MINUTES = 30

PURPOSE_VERIFY = "verify_email"
PURPOSE_RESET = "reset_password"

#: 会话「最后活跃时间」的写入节流：避免每个请求都写库
_TOUCH_INTERVAL = timedelta(minutes=5)


# ------------------------------------------------------------------ 用户查询


async def find_user_by_login(db: AsyncSession, login: str) -> User | None:
    """登录名或邮箱，二者皆可。"""
    login = (login or "").strip()
    if not login:
        return None
    stmt = select(User).where((User.username == login) | (User.email == login.lower()))
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_user_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == (email or "").strip().lower()))
    ).scalar_one_or_none()


async def username_taken(db: AsyncSession, username: str) -> bool:
    return (
        await db.execute(select(User.id).where(User.username == username.strip()))
    ).first() is not None


# ------------------------------------------------------------------ 会话


async def create_session(
    db: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
) -> str:
    """创建登录会话，返回写入 Cookie 的原始令牌（数据库只留哈希）。"""
    raw = new_token(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash(raw),
            csrf_token=new_csrf_token(),
            ip_address=ip,
            user_agent=(user_agent or "")[:512],
            expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        )
    )
    user.last_login_at = utcnow()
    user.last_login_ip = ip
    await db.commit()
    return raw


async def resolve_session(
    db: AsyncSession, raw_token: str | None
) -> tuple[User, UserSession] | None:
    if not raw_token:
        return None
    row = (
        await db.execute(
            select(UserSession).where(
                UserSession.token_hash == token_hash(raw_token),
                UserSession.revoked.is_(False),
                UserSession.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user, row


async def touch_session(db: AsyncSession, row: UserSession) -> None:
    if utcnow() - row.last_seen_at < _TOUCH_INTERVAL:
        return
    row.last_seen_at = utcnow()
    await db.commit()


async def revoke_session(db: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    await db.execute(
        update(UserSession)
        .where(UserSession.token_hash == token_hash(raw_token))
        .values(revoked=True)
    )
    await db.commit()


async def revoke_all_sessions(
    db: AsyncSession, user_id: int, *, except_token: str | None = None
) -> None:
    """改密码后强制其它设备下线（可保留当前设备）。"""
    stmt = update(UserSession).where(UserSession.user_id == user_id)
    if except_token:
        stmt = stmt.where(UserSession.token_hash != token_hash(except_token))
    await db.execute(stmt.values(revoked=True))
    await db.commit()


# ------------------------------------------------------------------ 邮箱令牌


async def issue_email_token(
    db: AsyncSession, user: User, purpose: str, ttl_minutes: int = VERIFY_TTL_MINUTES
) -> str:
    """签发一次性令牌，同时作废该用户同用途的旧令牌。"""
    await db.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user.id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
        .values(used_at=utcnow())
    )
    raw = new_token(32)
    db.add(
        EmailToken(
            user_id=user.id,
            token_hash=token_hash(raw),
            purpose=purpose,
            expires_at=utcnow() + timedelta(minutes=ttl_minutes),
        )
    )
    await db.commit()
    return raw


async def consume_email_token(db: AsyncSession, raw: str, purpose: str) -> User | None:
    if not raw:
        return None
    row = (
        await db.execute(
            select(EmailToken).where(
                EmailToken.token_hash == token_hash(raw),
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
                EmailToken.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    row.used_at = utcnow()
    user = await db.get(User, row.user_id)
    await db.commit()
    return user
