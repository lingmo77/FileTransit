"""站点配置（键值对）与跨进程任务锁。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: JSON 序列化后的值
    value: Mapped[str] = mapped_column(Text, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TaskLock(Base):
    """让定时任务在多 worker / 多进程下只执行一次。"""

    __tablename__ = "task_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
