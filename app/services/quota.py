"""配额校验与用量维护。

口径：统计未过期的有效文件；删除认领记录在物理清理成功前仍占用配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
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
    """短写事务中串行重算、复核和消费配额，调用方随后入库并提交。

    sync_usage 先取得 SQLite 写锁，直到文件入库一并提交才释放。
    不能使用请求鉴权时缓存的 User，也不能先添加文件再调用本函数。
    """
    await sync_usage(db, user_id)
    user = await db.get(User, user_id, populate_existing=True)
    if user is None:
        raise QuotaExceeded("用户已不存在")
    if not user.is_active:
        raise QuotaExceeded("用户已被停用，无法完成上传")
    await ensure_can_upload(db, user, size)
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


async def ensure_current_usage(db: AsyncSession, user_id: int) -> None:
    """续期把过期文件重新计入配额时，同样不能突破限额。"""
    await sync_usage(db, user_id)
    user = await db.get(User, user_id, populate_existing=True)
    info = await resolve_quota(db, user)
    if ((not info.unlimited_files and info.used_files > info.max_files)
            or (not info.unlimited_bytes and info.used_bytes > info.max_bytes)):
        raise QuotaExceeded("续期后将超过当前配额，请先删除部分文件")


async def sync_usage(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """按有效文件及尚未完成物理删除的记录重算用量。

    长期有效的文件（``expires_at`` 为 NULL）同样占用配额，必须一并计入，
    否则每次维护跑完用量都会被清零，等于白送配额。
    """
    # 必须先写后读，防止并发上传或后台重算用旧快照覆盖新用量。
    # SQLite 的写锁由调用方的 commit/rollback 释放，文件传输不在此事务内。
    await db.execute(update(User).where(User.id == user_id).values(id=User.id))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = (
        await db.execute(
            select(func.count(FileItem.id), func.coalesce(func.sum(FileItem.size), 0)).where(
                FileItem.user_id == user_id,
                or_(FileItem.is_deleted.is_(True), active_clause(now)),
            )
        )
    ).one()
    count, total = int(row[0] or 0), int(row[1] or 0)
    await db.execute(
        update(User).where(User.id == user_id).values(used_files=count, used_bytes=total)
    )
    return count, total
