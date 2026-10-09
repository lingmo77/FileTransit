"""配额校验与用量维护。

口径：只统计**未过期且未删除**的文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.templating import human_size
from ..models import UNLIMITED, FileItem, User
from . import settings_service
from .files import active_clause


@dataclass(slots=True)
class QuotaInfo:
    max_files: int  # -1 表示无限制
    max_bytes: int  # -1 表示无限制
    used_files: int
    used_bytes: int

    @property
    def unlimited_files(self) -> bool:
        return self.max_files == UNLIMITED

    @property
    def unlimited_bytes(self) -> bool:
        return self.max_bytes == UNLIMITED

    @property
    def remaining_files(self) -> int | None:
        return None if self.unlimited_files else max(0, self.max_files - self.used_files)

    @property
    def remaining_bytes(self) -> int | None:
        return None if self.unlimited_bytes else max(0, self.max_bytes - self.used_bytes)


class QuotaExceeded(Exception):
    def __init__(self, message: str, *, field: str = "quota") -> None:
        self.message = message
        self.field = field
        super().__init__(message)


async def resolve_quota(db: AsyncSession, user: User) -> QuotaInfo:
    """用户未单独设置时回落到站点默认配额。"""
    max_files = user.quota_max_files
    if max_files is None:
        max_files = await settings_service.get_int(db, "quota.default_max_files", 10)

    max_bytes = user.quota_max_bytes
    if max_bytes is None:
        max_bytes = await settings_service.get_int(db, "quota.default_max_bytes", 10 * 1024**3)

    return QuotaInfo(
        max_files=int(max_files),
        max_bytes=int(max_bytes),
        used_files=user.used_files,
        used_bytes=user.used_bytes,
    )


async def ensure_can_upload(db: AsyncSession, user: User, incoming_bytes: int) -> QuotaInfo:
    """上传前校验；不通过时抛 :class:`QuotaExceeded`。"""
    quota = await resolve_quota(db, user)

    if not quota.unlimited_files and quota.used_files + 1 > quota.max_files:
        raise QuotaExceeded(
            f"已达到同时分享文件数上限（{quota.max_files} 个），请先删除部分文件或等待其过期",
            field="files",
        )

    if not quota.unlimited_bytes and quota.used_bytes + incoming_bytes > quota.max_bytes:
        free = max(0, quota.max_bytes - quota.used_bytes)
        raise QuotaExceeded(
            f"存储空间不足：剩余 {human_size(free)}，本次需要 {human_size(incoming_bytes)}"
            f"（上限 {human_size(quota.max_bytes)}）",
            field="bytes",
        )

    return quota


async def consume(db: AsyncSession, user_id: int, size: int) -> None:
    """原子增加用量计数（与文件入库同一事务）。"""
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(used_files=User.used_files + 1, used_bytes=User.used_bytes + size)
    )


async def release(db: AsyncSession, user_id: int, size: int) -> None:
    """原子减少用量计数，不会低于 0。"""
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            used_files=func.max(User.used_files - 1, 0),
            used_bytes=func.max(User.used_bytes - size, 0),
        )
    )


async def sync_usage(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """按实际未过期文件重算用量，用于清理任务与后台修复。

    长期有效的文件（``expires_at`` 为 NULL）同样占用配额，必须一并计入，
    否则每次维护跑完用量都会被清零，等于白送配额。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = (
        await db.execute(
            select(func.count(FileItem.id), func.coalesce(func.sum(FileItem.size), 0)).where(
                FileItem.user_id == user_id,
                FileItem.is_deleted.is_(False),
                active_clause(now),
            )
        )
    ).one()
    count, total = int(row[0] or 0), int(row[1] or 0)
    await db.execute(
        update(User).where(User.id == user_id).values(used_files=count, used_bytes=total)
    )
    return count, total
