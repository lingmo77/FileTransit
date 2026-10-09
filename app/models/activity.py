"""操作日志（审计）。

日志不设外键：用户被删除后审计记录仍应保留，因此 ``username`` 做了冗余存储。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    #: 冗余用户名，未登录时为 "anonymous"
    username: Mapped[str] = mapped_column(String(64), default="anonymous", nullable=False)

    #: 见 services.activity.Action
    action: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="success", nullable=False)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    browser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device: Mapped[str | None] = mapped_column(String(32), nullable=True)

    method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: JSON 字符串，存放原始文件名、大小、失败原因等扩展信息
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_logs_created_at", "created_at"),
        Index("ix_logs_action_created", "action", "created_at"),
        Index("ix_logs_user_created", "user_id", "created_at"),
    )
