"""配额校验与用量维护。

口径：统计未过期的有效文件；删除认领记录在物理清理成功前仍占用配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.templating import human_size
from ..models import UNLIMITED, FileItem, OfflineTask, User
from . import settings_service
from .files import active_clause


@dataclass(slots=True)
class QuotaInfo:
    max_files: int  # -1 表示无限制
    max_bytes: int  # -1 表示无限制
    used_files: int
    used_bytes: int
    reserved_files: int = 0
    reserved_bytes: int = 0

    @property
    def unlimited_files(self) -> bool:
        return self.max_files == UNLIMITED

    @property
    def unlimited_bytes(self) -> bool:
        return self.max_bytes == UNLIMITED

    @property
    def remaining_files(self) -> int | None:
        return None if self.unlimited_files else max(0, self.max_files - self.used_files - self.reserved_files)

    @property
    def remaining_bytes(self) -> int | None:
        return None if self.unlimited_bytes else max(0, self.max_bytes - self.used_bytes - self.reserved_bytes)


class QuotaExceeded(Exception):
    def __init__(self, message: str, *, field: str = "quota") -> None:
        self.message = message
        self.field = field
        super().__init__(message)


async def reserved_usage(db: AsyncSession, user_id: int, *, exclude_task_id: str | None = None) -> tuple[int, int]:
    stmt = select(func.coalesce(func.sum(OfflineTask.reserved_files), 0),
                  func.coalesce(func.sum(OfflineTask.reserved_bytes), 0)).where(OfflineTask.user_id == user_id)
    if exclude_task_id is not None:
        stmt = stmt.where(OfflineTask.id != exclude_task_id)
    count, size = (await db.execute(stmt)).one()
    return int(count), int(size)


async def resolve_quota(db: AsyncSession, user: User, *, exclude_task_id: str | None = None) -> QuotaInfo:
    """用户未单独设置时回落到站点默认配额。"""
    max_files = user.quota_max_files
    if max_files is None:
        max_files = await settings_service.get_int(db, "quota.default_max_files", 10)

    max_bytes = user.quota_max_bytes
    if max_bytes is None:
        max_bytes = await settings_service.get_int(db, "quota.default_max_bytes", 10 * 1024**3)

    reserved_files, reserved_bytes = await reserved_usage(db, user.id, exclude_task_id=exclude_task_id)
    return QuotaInfo(
        max_files=int(max_files),
        max_bytes=int(max_bytes),
        used_files=user.used_files,
        used_bytes=user.used_bytes,
        reserved_files=reserved_files,
        reserved_bytes=reserved_bytes,
    )


async def ensure_can_upload(db: AsyncSession, user: User, incoming_bytes: int, *,
                            incoming_files: int = 1, exclude_task_id: str | None = None) -> QuotaInfo:
    """上传前校验；不通过时抛 :class:`QuotaExceeded`。"""
    quota = await resolve_quota(db, user, exclude_task_id=exclude_task_id)

    if not quota.unlimited_files and quota.used_files + quota.reserved_files + incoming_files > quota.max_files:
        raise QuotaExceeded(
            f"已达到同时分享文件数上限（{quota.max_files} 个），请先删除部分文件或等待其过期",
            field="files",
        )

    if not quota.unlimited_bytes and quota.used_bytes + quota.reserved_bytes + incoming_bytes > quota.max_bytes:
        free = quota.remaining_bytes or 0
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
    await consume_many(db, user_id, [size])


async def consume_many(db: AsyncSession, user_id: int, sizes: list[int], *,
                       exclude_task_id: str | None = None) -> None:
    """Consume a whole import in one transaction; caller inserts every FileItem then commits."""
    if not sizes or any(size < 0 for size in sizes):
        raise QuotaExceeded("文件大小或数量不合法")
    await sync_usage(db, user_id)
    user = await db.get(User, user_id, populate_existing=True)
    if user is None:
        raise QuotaExceeded("用户已不存在")
    if not user.is_active:
        raise QuotaExceeded("用户已被停用，无法完成上传")
    await ensure_can_upload(db, user, sum(sizes), incoming_files=len(sizes), exclude_task_id=exclude_task_id)
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(used_files=User.used_files + len(sizes), used_bytes=User.used_bytes + sum(sizes))
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
    if ((not info.unlimited_files and info.used_files + info.reserved_files > info.max_files)
            or (not info.unlimited_bytes and info.used_bytes + info.reserved_bytes > info.max_bytes)):
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
