"""文件查询与分页。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.templating import CATEGORY_EXTENSIONS
from ..models import FileItem, User, utcnow
from . import settings_service
from .pagination import DEFAULT_PER_PAGE, Page, paginate

PER_PAGE = DEFAULT_PER_PAGE

#: ``expires_hours`` 的哨兵值，表示长期有效（库里存 ``expires_at = NULL``）
FOREVER_HOURS = 0
#: 日志与页面上长期有效的展示文案
FOREVER_LABEL = "长期有效"

FILE_ORDER = (FileItem.created_at.desc(), FileItem.id.desc())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(stmt: Select, *, q: str | None, kind: str | None) -> Select:
    if q:
        stmt = stmt.where(FileItem.original_name.like(f"%{_escape_like(q)}%", escape="\\"))
    if kind and kind in CATEGORY_EXTENSIONS:
        stmt = stmt.where(
            or_(
                *[
                    FileItem.original_name.like(f"%.{ext}", escape="\\")
                    for ext in CATEGORY_EXTENSIONS[kind]
                ]
            )
        )
    return stmt


def _active_cutoff() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def active_clause(cutoff: datetime | None = None):
    """「仍然有效」的 SQL 条件：未过期，或长期有效（``expires_at`` 为 NULL）。

    SQL 里 NULL 参与比较一律求值为假，所以长期有效的文件若不显式用 IS NULL
    捞出来，会同时从「有效」和「已过期」两边消失——列表里看不到、配额里也不算。
    """
    return or_(
        FileItem.expires_at.is_(None),
        FileItem.expires_at > (cutoff or _active_cutoff()),
    )


class ForeverNotAllowed(Exception):
    """未获授权的用户试图设置长期有效期。"""


async def resolve_expiry(
    db: AsyncSession, expires_hours: int, *, allow_forever: bool
) -> datetime | None:
    """把请求里的保留时长换算成到期的 UTC 时间；``None`` 表示长期有效。

    普通用户的时长会被 ``quota.max_expire_days`` 钳制；只有 ``allow_forever``
    为真的用户（管理员，或被管理员勾选了 ``can_permanent`` 的账号）传入
    :data:`FOREVER_HOURS` 才会设为长期有效——**这里必须拦下来**，
    不能只靠前端把选项藏起来。
    """
    if expires_hours <= FOREVER_HOURS:
        if not allow_forever:
            raise ForeverNotAllowed
        return None

    max_days = await settings_service.get_int(db, "quota.max_expire_days", 30)
    hours = max(1, min(expires_hours, max(1, max_days * 24)))
    return utcnow() + timedelta(hours=hours)


async def list_public(
    db: AsyncSession,
    *,
    page: int = 1,
    per_page: int = PER_PAGE,
    q: str | None = None,
    kind: str | None = None,
) -> Page:
    """首页文件墙：所有用户的公开且未过期文件。"""
    stmt = select(FileItem).where(
        FileItem.is_deleted.is_(False),
        FileItem.is_public.is_(True),
        active_clause(),
    )
    stmt = _apply_filters(stmt, q=q, kind=kind)
    return await paginate(
        db,
        stmt,
        page=page,
        per_page=per_page,
        options=(selectinload(FileItem.owner),),
        order_by=FILE_ORDER,
    )


async def list_for_user(
    db: AsyncSession,
    user: User,
    *,
    page: int = 1,
    per_page: int = PER_PAGE,
    q: str | None = None,
    kind: str | None = None,
) -> Page:
    """我的文件：包含已过期的，方便用户看到"即将被清理"。"""
    stmt = select(FileItem).where(
        FileItem.user_id == user.id,
        FileItem.is_deleted.is_(False),
    )
    stmt = _apply_filters(stmt, q=q, kind=kind)
    return await paginate(
        db,
        stmt,
        page=page,
        per_page=per_page,
        options=(selectinload(FileItem.owner),),
        order_by=FILE_ORDER,
    )


async def list_admin(
    db: AsyncSession,
    *,
    page: int = 1,
    per_page: int = PER_PAGE,
    q: str | None = None,
    user_id: int | None = None,
    expired: bool = False,
) -> Page:
    """后台文件管理：全站文件。"""
    stmt = select(FileItem).where(FileItem.is_deleted.is_(False))
    if user_id:
        stmt = stmt.where(FileItem.user_id == user_id)
    if expired:
        stmt = stmt.where(FileItem.expires_at <= _active_cutoff())
    else:
        stmt = stmt.where(active_clause())
    stmt = _apply_filters(stmt, q=q, kind=None)
    return await paginate(
        db,
        stmt,
        page=page,
        per_page=per_page,
        options=(selectinload(FileItem.owner),),
        order_by=FILE_ORDER,
    )


async def get_by_public_id(db: AsyncSession, public_id: str) -> FileItem | None:
    return (
        await db.execute(
            select(FileItem)
            .options(selectinload(FileItem.owner))
            .where(FileItem.public_id == public_id, FileItem.is_deleted.is_(False))
        )
    ).scalar_one_or_none()


async def get_by_id(db: AsyncSession, file_id: int) -> FileItem | None:
    return (
        await db.execute(
            select(FileItem)
            .options(selectinload(FileItem.owner))
            .where(FileItem.id == file_id, FileItem.is_deleted.is_(False))
        )
    ).scalar_one_or_none()


async def public_summary(db: AsyncSession) -> tuple[int, int]:
    """首页小标题用：公开文件数量与总体积。"""
    from sqlalchemy import func

    row = (
        await db.execute(
            select(func.count(FileItem.id), func.coalesce(func.sum(FileItem.size), 0)).where(
                FileItem.is_deleted.is_(False),
                FileItem.is_public.is_(True),
                active_clause(),
            )
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def expiring_soon_count(db: AsyncSession, user_id: int, hours: int = 24) -> int:
    from datetime import timedelta

    from sqlalchemy import func

    deadline = utcnow() + timedelta(hours=hours)
    return int(
        (
            await db.execute(
                select(func.count(FileItem.id)).where(
                    FileItem.user_id == user_id,
                    FileItem.is_deleted.is_(False),
                    FileItem.expires_at > utcnow(),
                    FileItem.expires_at <= deadline,
                )
            )
        ).scalar_one()
        or 0
    )


__all__ = [
    "PER_PAGE",
    "FOREVER_HOURS",
    "FOREVER_LABEL",
    "ForeverNotAllowed",
    "active_clause",
    "resolve_expiry",
    "get_by_id",
    "get_by_public_id",
    "list_admin",
    "list_for_user",
    "list_public",
    "public_summary",
    "expiring_soon_count",
    "Page",
    "User",
]
