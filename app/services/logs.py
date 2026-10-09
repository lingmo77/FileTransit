"""操作日志的查询、导出与清理。"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.timeutil import DEFAULT_TIMEZONE, local_to_utc, to_local
from ..models import ActivityLog
from .activity import ACTION_LABELS
from .pagination import DEFAULT_PER_PAGE, Page, paginate
from .settings_service import get_str

LOGS_PER_PAGE = 50
EXPORT_LIMIT = 200_000


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_date(
    value: str | None, *, tz_name: str | None = None, end_of_day: bool = False
) -> datetime | None:
    """把用户填写的日期解析成 naive UTC。

    接受 ``YYYY-MM-DD`` 或完整 ISO 时间。纯日期按**站点时区**的壁钟时间理解
    （``end_of_day`` 取当天 23:59:59），再换算成 UTC——库里存的是 UTC，
    少这一步换算就会整体偏移一个时区差，表现为"填了日期却筛不到、删不掉"。
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d")
            if end_of_day:
                parsed = parsed + timedelta(days=1) - timedelta(seconds=1)
            return local_to_utc(parsed, tz_name)
        # 带偏移的 ISO 串会被 astimezone 正确换算；不带偏移的按站点时区解释
        return local_to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")), tz_name)
    except ValueError:
        return None


async def site_tz(db: AsyncSession) -> str:
    """当前站点的时区名，用于把用户填写的日期换算成 UTC。"""
    return await get_str(db, "site.timezone", DEFAULT_TIMEZONE)


def _filtered(
    *,
    q: str | None = None,
    action: str | None = None,
    status_filter: str | None = None,
    user_id: int | None = None,
    ip: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    stmt = select(ActivityLog)
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                ActivityLog.username.like(pattern, escape="\\"),
                ActivityLog.path.like(pattern, escape="\\"),
                ActivityLog.detail.like(pattern, escape="\\"),
                ActivityLog.target_id.like(pattern, escape="\\"),
            )
        )
    if action:
        stmt = stmt.where(ActivityLog.action == action)
    if status_filter:
        stmt = stmt.where(ActivityLog.status == status_filter)
    if user_id:
        stmt = stmt.where(ActivityLog.user_id == user_id)
    if ip:
        stmt = stmt.where(ActivityLog.ip_address.like(f"%{_escape_like(ip)}%", escape="\\"))
    if start:
        stmt = stmt.where(ActivityLog.created_at >= start)
    if end:
        stmt = stmt.where(ActivityLog.created_at <= end)
    return stmt


async def list_logs(
    db: AsyncSession,
    *,
    page: int = 1,
    per_page: int = LOGS_PER_PAGE,
    **filters,
) -> Page:
    stmt = _filtered(**filters)
    return await paginate(
        db,
        stmt,
        page=page,
        per_page=per_page,
        order_by=(ActivityLog.created_at.desc(), ActivityLog.id.desc()),
    )


async def action_counts(db: AsyncSession) -> dict[str, int]:
    """按动作类型统计日志条数，供后台下拉框与筛选项展示。"""
    rows = (
        await db.execute(
            select(ActivityLog.action, func.count(ActivityLog.id))
            .group_by(ActivityLog.action)
            .order_by(func.count(ActivityLog.id).desc())
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def purge_logs(
    db: AsyncSession,
    *,
    before: datetime | None = None,
    after: datetime | None = None,
    action: str | None = None,
    status_filter: str | None = None,
) -> int:
    """按条件清空日志。全部条件为空表示清空所有日志。"""
    stmt = delete(ActivityLog)
    if before:
        stmt = stmt.where(ActivityLog.created_at < before)
    if after:
        stmt = stmt.where(ActivityLog.created_at > after)
    if action:
        stmt = stmt.where(ActivityLog.action == action)
    if status_filter:
        stmt = stmt.where(ActivityLog.status == status_filter)

    result = await db.execute(stmt)
    await db.commit()
    return int(result.rowcount or 0)


def export_csv(rows: list[ActivityLog], tz_name: str | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            f"时间({tz_name or DEFAULT_TIMEZONE})",
            "用户",
            "用户ID",
            "动作",
            "结果",
            "IP",
            "浏览器",
            "操作系统",
            "设备",
            "方法",
            "路径",
            "对象",
            "详情",
        ]
    )
    for row in rows:
        detail = row.detail
        if detail:
            try:
                detail = json.dumps(json.loads(detail), ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        writer.writerow(
            [
                (to_local(row.created_at, tz_name) or row.created_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                row.username,
                row.user_id or "",
                ACTION_LABELS.get(row.action, row.action),
                "成功" if row.status == "success" else "失败",
                row.ip_address or "",
                row.browser or "",
                row.os_name or "",
                row.device or "",
                row.method or "",
                row.path or "",
                f"{row.target_type or ''}:{row.target_id or ''}".strip(":"),
                detail or "",
            ]
        )
    return buffer.getvalue()


async def fetch_for_export(db: AsyncSession, **filters) -> list[ActivityLog]:
    stmt = _filtered(**filters).order_by(ActivityLog.created_at.desc()).limit(EXPORT_LIMIT)
    return list((await db.execute(stmt)).scalars().all())


__all__ = [
    "ACTION_LABELS",
    "DEFAULT_PER_PAGE",
    "EXPORT_LIMIT",
    "LOGS_PER_PAGE",
    "Page",
    "action_counts",
    "export_csv",
    "fetch_for_export",
    "list_logs",
    "parse_date",
    "purge_logs",
]
