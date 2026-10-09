"""ORM 基类与时间工具。

约定：数据库中的时间一律为 **不带时区的 UTC**，展示时再按站点时区转换。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """当前 UTC 时间（naive，便于 SQLite 存储与比较）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass
