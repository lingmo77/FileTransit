"""文件元数据。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class FileItem(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 对外分享码（URL 中使用），避免自增 id 被枚举
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 相对于 storage_dir 的路径，例如 ``ab/cd/9f3c...``
    stored_path: Mapped[str] = mapped_column(String(255), nullable=False)

    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream", nullable=False
    )
    #: 上传时计算的摘要，可用于校验下载完整性（大文件可选算）
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 软删除标记；清理任务会物理删除文件与记录
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 记录该文件的下载是否被计入日志（站点设置可关闭匿名下载日志）
    uploaded_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: 到期时间（naive UTC）；``None`` 表示长期有效，只有管理员能设置
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)

    owner: Mapped["User"] = relationship(back_populates="files")  # noqa: F821

    __table_args__ = (
        Index("ix_files_expires_at_active", "expires_at", "is_deleted"),
        Index("ix_files_created_at", "created_at"),
        Index("ix_files_user_expires", "user_id", "expires_at"),
        Index("ix_files_public_list", "is_public", "is_deleted", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        # 长期有效（expires_at 为 None）永远不算过期
        if self.expires_at is None:
            return False
        return self.expires_at <= utcnow()
