"""站点配置的读写与缓存。

配置放在 ``settings`` 表里（值以 JSON 存储），后台改动立即生效、无需重启。
进程内有一份缓存并带 TTL，多 worker 部署时其它进程最多 ``_CACHE_TTL`` 秒后同步。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from fastapi import Request

from ..core.config import settings as env_settings
from ..core.crypto import decrypt, encrypt
from ..models import Setting

# ------------------------------------------------------------------ 默认值

GB = 1024**3

DEFAULTS: dict[str, Any] = {
    # 站点信息
    "site.name": "文件中转站",
    "site.subtitle": "临时文件中转，到期自动销毁",
    "site.footer": "",
    "site.default_theme": "system",  # light | dark | system
    "site.timezone": "Asia/Shanghai",
    "site.announcement": "",
    # 站点根地址，用于拼接邮件中的验证链接；留空则按请求自动推断
    "site.base_url": "",
    # 行为开关
    "site.allow_guest_download": True,
    "site.log_anonymous_download": True,
    "registration.enabled": True,
    "registration.require_email_verification": True,
    # 配额与有效期
    "quota.default_max_files": 10,
    "quota.default_max_bytes": 10 * GB,
    "quota.max_expire_days": 30,
    "quota.max_upload_mb": env_settings.max_upload_mb,
    # 邮件服务器
    "email.smtp_host": "",
    "email.smtp_port": 465,
    "email.encryption": "ssl",  # none | ssl | starttls
    "email.username": "",
    "email.password_enc": "",  # 加密存储
    "email.from_name": "",
    "email.from_address": "",
    "email.timeout": 20,
    # 日志
    "logs.retention_days": 0,  # 0 = 不自动清理
    "logs.max_rows": 500000,
}

#: 这些键的值不允许通过后台接口返回给前端
SECRET_KEYS = frozenset({"email.password_enc"})

_CACHE_TTL = 15.0
_cache: dict[str, Any] = {}
_cache_at: float = 0.0


def _coerce(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


async def load_all(db: AsyncSession) -> dict[str, Any]:
    global _cache, _cache_at
    rows = (await db.execute(select(Setting.key, Setting.value))).all()
    values = dict(DEFAULTS)
    for key, value in rows:
        values[key] = _coerce(value)
    _cache = values
    _cache_at = time.monotonic()
    return values


async def all_values(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    if force or not _cache or (time.monotonic() - _cache_at) > _CACHE_TTL:
        return await load_all(db)
    return _cache


async def get(db: AsyncSession, key: str, default: Any = None) -> Any:
    values = await all_values(db)
    if key in values:
        return values[key]
    return DEFAULTS.get(key, default)


async def get_str(db: AsyncSession, key: str, default: str = "") -> str:
    value = await get(db, key)
    return str(value) if value is not None else default


async def get_int(db: AsyncSession, key: str, default: int = 0) -> int:
    try:
        return int(await get(db, key))
    except (TypeError, ValueError):
        return default


async def get_bool(db: AsyncSession, key: str, default: bool = False) -> bool:
    value = await get(db, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


async def set_many(db: AsyncSession, values: dict[str, Any], *, updated_by: int | None = None) -> None:
    for key, value in values.items():
        payload = json.dumps(value, ensure_ascii=False)
        row = await db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=payload, updated_by=updated_by))
        else:
            row.value = payload
            row.updated_by = updated_by
    await db.commit()
    await load_all(db)


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


# ------------------------------------------------------------------ 邮件配置


def encrypt_secret(plaintext: str) -> str:
    return encrypt(plaintext, env_settings.secret_key)


def decrypt_secret(token: str) -> str:
    return decrypt(token, env_settings.secret_key)


async def smtp_configured(db: AsyncSession) -> bool:
    host = await get_str(db, "email.smtp_host")
    sender = await get_str(db, "email.from_address")
    return bool(host and sender)


async def template_settings(db: AsyncSession) -> dict[str, Any]:
    """模板里用得到的扁平配置，避免在模板中写 ``site['site.name']`` 这种下标。"""
    values = await all_values(db)

    def _b(key: str, default: bool) -> bool:
        value = values.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _i(key: str, default: int) -> int:
        try:
            return int(values.get(key, default))
        except (TypeError, ValueError):
            return default

    host = str(values.get("email.smtp_host") or "").strip()
    sender = str(values.get("email.from_address") or "").strip()

    return {
        "name": str(values.get("site.name") or "文件中转站"),
        "subtitle": str(values.get("site.subtitle") or ""),
        "footer": str(values.get("site.footer") or ""),
        "announcement": str(values.get("site.announcement") or ""),
        "default_theme": str(values.get("site.default_theme") or "system"),
        "timezone": str(values.get("site.timezone") or "Asia/Shanghai"),
        "base_url": str(values.get("site.base_url") or ""),
        "allow_guest_download": _b("site.allow_guest_download", True),
        "registration_enabled": _b("registration.enabled", True),
        "require_email_verification": _b("registration.require_email_verification", True),
        "max_expire_days": _i("quota.max_expire_days", 30),
        "default_max_files": _i("quota.default_max_files", 10),
        "default_max_bytes": _i("quota.default_max_bytes", 10 * GB),
        "max_upload_mb": _i("quota.max_upload_mb", env_settings.max_upload_mb),
        "smtp_configured": bool(host and sender),
    }


async def base_url(db: AsyncSession, request: Request | None = None) -> str:
    """站点对外根地址：优先用后台配置，其次按请求推断。"""
    configured = (await get_str(db, "site.base_url")).strip()
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""
