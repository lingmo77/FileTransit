"""后台仪表盘所需的统计聚合。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ActivityLog, FileItem, User, utcnow
from . import storage
from .activity import Action
from .files import active_clause


def _day_start(days_ago: int = 0) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=days_ago)


async def overview(db: AsyncSession) -> dict[str, object]:
    now = utcnow()

    users_total = int((await db.execute(select(func.count(User.id)))).scalar_one() or 0)
    users_active = int(
        (await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))).scalar_one()
        or 0
    )
    users_pending = int(
        (await db.execute(select(func.count(User.id)).where(User.email_verified.is_(False)))).scalar_one()
        or 0
    )

    files_active = int(
        (
            await db.execute(
                select(func.count(FileItem.id)).where(
                    FileItem.is_deleted.is_(False), active_clause(now)
                )
            )
        ).scalar_one()
        or 0
    )
    files_bytes = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(FileItem.size), 0)).where(
                    FileItem.is_deleted.is_(False), active_clause(now)
                )
            )
        ).scalar_one()
        or 0
    )
    files_expired = int(
        (
            await db.execute(
                select(func.count(FileItem.id)).where(
                    FileItem.is_deleted.is_(False), FileItem.expires_at <= now
                )
            )
        ).scalar_one()
        or 0
    )
    downloads_total = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(FileItem.download_count), 0)).where(
                    FileItem.is_deleted.is_(False)
                )
            )
        ).scalar_one()
        or 0
    )

    uploads_today = int(
        (
            await db.execute(
                select(func.count(ActivityLog.id)).where(
                    ActivityLog.action == Action.UPLOAD,
                    ActivityLog.status == "success",
                    ActivityLog.created_at >= _day_start(),
                )
            )
        ).scalar_one()
        or 0
    )
    downloads_today = int(
        (
            await db.execute(
                select(func.count(ActivityLog.id)).where(
                    ActivityLog.action == Action.DOWNLOAD, ActivityLog.created_at >= _day_start()
                )
            )
        ).scalar_one()
        or 0
    )

    # 近 7 日趋势
    since = _day_start(6)
    rows = (
        await db.execute(
            select(
                func.date(ActivityLog.created_at).label("day"),
                ActivityLog.action,
                func.count(ActivityLog.id),
            )
            .where(
                ActivityLog.created_at >= since,
                ActivityLog.action.in_([Action.UPLOAD, Action.DOWNLOAD]),
            )
            .group_by("day", ActivityLog.action)
        )
    ).all()

    buckets: dict[str, dict[str, int]] = {}
    for offset in range(7):
        key = (_day_start(6 - offset)).strftime("%Y-%m-%d")
        buckets[key] = {"uploads": 0, "downloads": 0}
    for day, action, count in rows:
        key = str(day)
        if key in buckets:
            buckets[key]["uploads" if action == Action.UPLOAD else "downloads"] = int(count)

    trend = [{"date": key, **value} for key, value in buckets.items()]
    peak = max([1, *(item["uploads"] for item in trend), *(item["downloads"] for item in trend)])
    for item in trend:
        item["upload_pct"] = round(item["uploads"] * 100 / peak)
        item["download_pct"] = round(item["downloads"] * 100 / peak)

    top_users = (
        await db.execute(
            select(User.username, User.used_files, User.used_bytes)
            .where(User.used_files > 0)
            .order_by(User.used_bytes.desc())
            .limit(8)
        )
    ).all()

    recent = (
        await db.execute(
            select(FileItem)
            .where(FileItem.is_deleted.is_(False))
            .order_by(FileItem.created_at.desc())
            .limit(8)
        )
    ).scalars().all()

    disk_total, disk_used, disk_free = await storage.disk_usage()

    return {
        "users_total": users_total,
        "users_active": users_active,
        "users_pending": users_pending,
        "files_active": files_active,
        "files_bytes": files_bytes,
        "files_expired": files_expired,
        "downloads_total": downloads_total,
        "uploads_today": uploads_today,
        "downloads_today": downloads_today,
        "trend": trend,
        "top_users": [{"username": r[0], "files": r[1], "bytes": r[2]} for r in top_users],
        "recent_files": list(recent),
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_used_pct": round(disk_used * 100 / disk_total) if disk_total else 0,
    }
