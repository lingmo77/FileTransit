"""后台用户管理查询。"""

from __future__ import annotations

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from .pagination import DEFAULT_PER_PAGE, Page, paginate

USERS_PER_PAGE = 20


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filtered(q: str | None, status: str | None) -> Select:
    stmt = select(User)
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                User.username.like(pattern, escape="\\"),
                User.email.like(pattern, escape="\\"),
            )
        )
    if status == "active":
        stmt = stmt.where(User.is_active.is_(True))
    elif status == "disabled":
        stmt = stmt.where(User.is_active.is_(False))
    elif status == "pending":
        stmt = stmt.where(User.email_verified.is_(False))
    elif status == "admin":
        stmt = stmt.where(User.is_admin.is_(True))
    return stmt


async def list_users(
    db: AsyncSession,
    *,
    page: int = 1,
    per_page: int = USERS_PER_PAGE,
    q: str | None = None,
    status: str | None = None,
) -> Page:
    stmt = _filtered(q, status)
    return await paginate(
        db,
        stmt,
        page=page,
        per_page=per_page,
        order_by=(User.created_at.desc(), User.id.desc()),
    )


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def count_admins(db: AsyncSession, *, exclude_id: int | None = None) -> int:
    from sqlalchemy import func

    stmt = select(func.count(User.id)).where(User.is_admin.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int((await db.execute(stmt)).scalar_one() or 0)


async def delete_user(db: AsyncSession, user: User) -> tuple[int, int]:
    """删除用户及其全部文件（磁盘 + 记录）。返回 (文件数, 释放字节数)。"""
    from ..models import FileItem
    from . import storage

    files = (
        (await db.execute(select(FileItem).where(FileItem.user_id == user.id)))
        .scalars()
        .all()
    )
    freed = 0
    for item in files:
        try:
            await storage.delete_object(item.stored_path)
        except Exception:  # noqa: BLE001
            pass
        freed += item.size

    await db.delete(user)
    await db.commit()
    return len(files), freed


__all__ = [
    "DEFAULT_PER_PAGE",
    "Page",
    "USERS_PER_PAGE",
    "count_admins",
    "delete_user",
    "get_user",
    "list_users",
]
