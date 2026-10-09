"""用户、会话、邮箱验证令牌。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

#: 配额字段取该值表示"无限制"
UNLIMITED = -1


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 默认管理员首次登录后强制改密
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 是否允许上传永久保存（长期有效）的文件；管理员不需要这个开关，天然允许
    can_permanent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: NULL 表示使用站点默认配额
    quota_max_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_max_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: 冗余计数，便于上传前快速校验配额（只统计未过期文件）
    used_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    files: Mapped[list["FileItem"]] = relationship(  # noqa: F821
        back_populates="owner", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        return self.username

    @property
    def can_save_forever(self) -> bool:
        """能否把文件设为长期有效（永久保存）。

        管理员不用逐个授权，天然具备；普通用户看 ``can_permanent``。
        判权限的地方一律走这里，别再去读 ``is_admin``——否则新增授权方式时
        会漏掉某一处，前端放行了后端还拦着。
        """
        return self.is_admin or self.can_permanent


class UserSession(Base):
    """登录会话。服务端保留记录，因此可以强制下线与审计。"""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: 只存令牌的 SHA-256，泄露数据库也无法冒用会话
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship()


class EmailToken(Base):
    """邮箱验证 / 找回密码的一次性令牌。"""

    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    #: verify_email | reset_password
    purpose: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
