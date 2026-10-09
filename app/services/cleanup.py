"""后台定时维护：过期文件清理、临时文件清理、会话/令牌清理、日志瘦身。

多 worker 部署时每个进程都会起调度器，因此用 ``task_locks`` 表做跨进程互斥，
保证同一时刻只有一个进程真正执行维护逻辑。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..core.config import settings
from ..core.database import SessionLocal
from ..models import ActivityLog, EmailToken, FileItem, TaskLock, UserSession, utcnow
from . import quota, storage
from .activity import Action, log_activity
from . import settings_service

logger = logging.getLogger(__name__)

_LOCK_NAME = "maintenance"
_scheduler: AsyncIOScheduler | None = None


# ------------------------------------------------------------------ 任务锁


async def acquire_lock(db, name: str, ttl_seconds: int) -> bool:
    now = utcnow()
    until = now + timedelta(seconds=ttl_seconds)
    result = await db.execute(
        update(TaskLock)
        .where(
            TaskLock.name == name,
            or_(TaskLock.locked_until.is_(None), TaskLock.locked_until < now),
        )
        .values(locked_until=until)
    )
    if result.rowcount:
        await db.commit()
        return True

    db.add(TaskLock(name=name, locked_until=until))
    try:
        await db.commit()
        return True
    except IntegrityError:
        await db.rollback()
        return False


# ------------------------------------------------------------------ 过期文件


async def purge_expired_files(*, batch_size: int = 500) -> int:
    """删除所有已过期文件（磁盘 + 记录），并重算受影响用户的用量。"""
    now = utcnow()
    total = 0
    affected_users: set[int] = set()

    while True:
        async with SessionLocal() as db:
            files = (
                (
                    await db.execute(
                        select(FileItem)
                        .where(FileItem.expires_at <= now)
                        .order_by(FileItem.expires_at)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not files:
                break

            for item in files:
                affected_users.add(item.user_id)
                try:
                    await storage.delete_object(item.stored_path)
                except Exception:  # noqa: BLE001 - 单个文件删不掉不应中断整批
                    logger.warning("删除文件失败: %s", item.stored_path, exc_info=True)
                await db.delete(item)

            await db.commit()
            total += len(files)

    if affected_users:
        async with SessionLocal() as db:
            for user_id in affected_users:
                await quota.sync_usage(db, user_id)
            await db.commit()

    if total:
        logger.info("过期清理：删除 %s 个文件", total)
        await log_activity(
            action=Action.EXPIRE_CLEANUP,
            username="system",
            status="success",
            detail={"removed_files": total, "affected_users": len(affected_users)},
        )
    return total


# ------------------------------------------------------------------ 其它清理


async def purge_stale_sessions() -> int:
    now = utcnow()
    async with SessionLocal() as db:
        result = await db.execute(delete(UserSession).where(UserSession.expires_at < now))
        await db.execute(
            delete(EmailToken).where(
                or_(EmailToken.expires_at < now, EmailToken.used_at.is_not(None))
            )
        )
        await db.commit()
        return result.rowcount or 0


async def trim_logs() -> int:
    """按站点设置做日志瘦身：先按保留天数，再按最大条数。"""
    removed = 0
    async with SessionLocal() as db:
        retention_days = await settings_service.get_int(db, "logs.retention_days", 0)
        if retention_days > 0:
            cutoff = utcnow() - timedelta(days=retention_days)
            result = await db.execute(delete(ActivityLog).where(ActivityLog.created_at < cutoff))
            removed += result.rowcount or 0

        max_rows = await settings_service.get_int(db, "logs.max_rows", 500_000)
        if max_rows > 0:
            total = int((await db.execute(select(func.count(ActivityLog.id)))).scalar_one())
            if total > max_rows:
                cutoff_id = (
                    await db.execute(
                        select(ActivityLog.id)
                        .order_by(ActivityLog.id.desc())
                        .offset(max_rows - 1)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if cutoff_id is not None:
                    result = await db.execute(
                        delete(ActivityLog).where(ActivityLog.id < cutoff_id)
                    )
                    removed += result.rowcount or 0

        await db.commit()
    return removed


async def run_maintenance() -> dict[str, int]:
    """一次完整的维护流程（带跨进程锁）。"""
    async with SessionLocal() as db:
        locked = await acquire_lock(db, _LOCK_NAME, ttl_seconds=300)
    if not locked:
        return {}

    stats = {
        "expired_files": await purge_expired_files(),
        "tmp_files": await storage.cleanup_tmp_dir(),
        "sessions": await purge_stale_sessions(),
        "logs": await trim_logs(),
    }
    return stats


def _vacuum_sync() -> None:
    """VACUUM 必须在自动提交模式下执行，否则会报 "cannot VACUUM from within a transaction"。"""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        settings.sync_db_url, isolation_level="AUTOCOMMIT", poolclass=NullPool, future=True
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("VACUUM")
    finally:
        engine.dispose()


async def vacuum_database() -> None:
    """回收删除日志/文件后残留的磁盘空间。"""
    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(_vacuum_sync)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=timezone.utc)
    _scheduler.add_job(
        run_maintenance,
        "interval",
        minutes=max(1, settings.cleanup_interval_minutes),
        id=_LOCK_NAME,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )
    _scheduler.start()
    logger.info("维护调度器已启动，间隔 %s 分钟", settings.cleanup_interval_minutes)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
