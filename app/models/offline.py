"""Persistent offline intent, engine transfers and immutable import identities."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class OfflineTask(Base):
    __tablename__ = "offline_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    info_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_hours: Mapped[int] = mapped_column(Integer, default=336, nullable=False)
    reserved_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    download_speed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    upload_speed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    connections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seed_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "request_key", name="uq_offline_task_request"),
        Index("ix_offline_tasks_user_created", "user_id", "created_at"),
        Index("ix_offline_tasks_state", "state"),
        Index("ix_offline_tasks_info_hash", "info_hash"),
    )


class OfflineTransfer(Base):
    __tablename__ = "offline_task_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("offline_tasks.id", ondelete="CASCADE"), nullable=False)
    gid: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    parent_gid: Mapped[str | None] = mapped_column(String(16), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="payload", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (Index("ix_offline_transfers_task", "task_id"),)


class OfflineTaskFile(Base):
    __tablename__ = "offline_task_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("offline_tasks.id", ondelete="CASCADE"), nullable=False)
    file_index: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    completed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    stored_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", ondelete="SET NULL"), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("task_id", "file_index", name="uq_offline_file_index"),
        Index("ix_offline_task_files_file_id", "file_id"),
    )
