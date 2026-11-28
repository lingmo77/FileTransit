"""匿名上传：未登录用户上传文件的开关、限额与日志。

匿名文件都挂在系统账号 ``@anonymous`` 名下（迁移 b5e1d7c3a9f4 建好），文件表、到期清理、下载
都沿用登录用户的逻辑。防止被批量上传占满空间靠几层限制，全部可在后台「匿名上传」里调整：

* 单文件上限、最长保留 24 小时；
* 每个 IP 24 小时内的文件数和总量、全站每小时的文件数、匿名文件同时占用的总空间——
  按数据库里的上传日志统计，多 worker 之间也准确，到期删掉的文件仍然计入；
* 每个 IP 同时只接收 1 个上传，全站同时接收的匿名上传数有上限；
* 进程内的请求频率限制，挡住短时间内的大量请求。

IPv6 按 /64 网段统计（同一用户通常分到整个 /64，逐个换地址不能绕过限额）。

每次匿名上传都写一条日志：成功是 ``anon_upload``，与文件记录在同一个事务里提交，
所以统计不会漏算；被拒绝是 ``anon_upload_rejected``，写明原因。同一 IP 同一原因
60 秒内只记一条，其余次数累计到下一条的 ``suppressed`` 里，避免被刷请求时日志暴涨。
"""

from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings as env_settings
from ..core.security import RateLimiter, client_ip, new_token, user_agent
from ..models import ActivityLog, FileItem, User, utcnow
from . import quota, settings_service, storage
from .activity import Action, build_record, log_activity

ANONYMOUS_USERNAME = "@anonymous"
#: 写进日志 username 列的名字，与下载日志里的匿名访问一致
LOG_USERNAME = "anonymous"
MB = 1024 * 1024

#: 每个 IP 每 10 分钟最多发起的匿名上传请求数（含被拒绝的），在数据库统计之前先挡掉请求风暴
request_limiter = RateLimiter(30, 600, enabled=env_settings.rate_limit_enabled)

_system_user_id: int | None = None


class Rejected(Exception):
    """匿名上传被拒绝；``reason`` 写进日志，``message`` 返回给用户。"""

    def __init__(self, status_code: int, reason: str, message: str) -> None:
        self.status_code = status_code
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Limits:
    enabled: bool
    max_bytes: int
    max_expire_hours: int
    ip_daily_files: int
    ip_daily_bytes: int
    hourly_files: int
    total_bytes: int
    max_concurrent: int
    list_on_home: bool


async def limits(db: AsyncSession) -> Limits:
    get_int = settings_service.get_int
    return Limits(
        enabled=await settings_service.get_bool(db, "anon.upload_enabled", False),
        max_bytes=max(1, await get_int(db, "anon.max_upload_mb", 100)) * MB,
        max_expire_hours=max(1, min(24, await get_int(db, "anon.max_expire_hours", 24))),
        ip_daily_files=max(0, await get_int(db, "anon.ip_daily_files", 10)),
        ip_daily_bytes=max(0, await get_int(db, "anon.ip_daily_mb", 1024)) * MB,
        hourly_files=max(0, await get_int(db, "anon.hourly_files", 100)),
        total_bytes=max(0, await get_int(db, "anon.total_max_mb", 10240)) * MB,
        max_concurrent=max(1, await get_int(db, "anon.max_concurrent", 2)),
        list_on_home=await settings_service.get_bool(db, "anon.list_on_home", False),
    )


async def system_user_id(db: AsyncSession) -> int:
    """匿名文件所属的系统账号。"""
    global _system_user_id
    if _system_user_id is None:
        found = (await db.execute(select(User.id).where(
            User.username == ANONYMOUS_USERNAME, User.is_system.is_(True)))).scalar_one_or_none()
        if found is None:
            raise RuntimeError("缺少匿名上传的系统账号，请确认数据库已迁移到最新版本")
        _system_user_id = found
    return _system_user_id


def limit_key(ip: str) -> str:
    """统计限额用的地址：IPv4 原样，IPv6 取所在 /64 网段。"""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


# ------------------------------------------------------------------ 统计


async def _ip_usage(db: AsyncSession, key: str) -> tuple[int, int]:
    """这个 IP（网段）最近 24 小时成功上传的文件数和总字节数。"""
    since = utcnow() - timedelta(hours=24)
    rows = (await db.execute(select(ActivityLog.detail).where(
        ActivityLog.action == Action.ANON_UPLOAD, ActivityLog.status == "success",
        ActivityLog.ip_address == key, ActivityLog.created_at >= since))).scalars().all()
    total = 0
    for raw in rows:
        try:
            total += int(json.loads(raw or "{}").get("size", 0))
        except (TypeError, ValueError):
            continue
    return len(rows), total


async def _hourly_files(db: AsyncSession) -> int:
    since = utcnow() - timedelta(hours=1)
    return int((await db.execute(select(func.count(ActivityLog.id)).where(
        ActivityLog.action == Action.ANON_UPLOAD, ActivityLog.status == "success",
        ActivityLog.created_at >= since))).scalar_one() or 0)


async def check(db: AsyncSession, key: str, incoming: int, lim: Limits, *, stored_bytes: int | None = None) -> int:
    """不满足任一限额时抛 :class:`Rejected`；否则返回本次最多还能接收的字节数。

    ``stored_bytes`` 是匿名文件当前占用的空间；不传时读系统账号上的计数（上传前的预检）。
    入库前会在写锁内用重算后的准确值再查一遍。
    """
    if not lim.enabled:
        raise Rejected(403, "disabled", "本站未开放匿名上传，请登录后上传")
    if incoming > lim.max_bytes:
        raise Rejected(413, "too_large", f"匿名上传单个文件不能超过 {storage_size(lim.max_bytes)}")
    files, used = await _ip_usage(db, key)
    if files + 1 > lim.ip_daily_files:
        raise Rejected(429, "ip_daily_files",
                       f"同一网络 24 小时内最多匿名上传 {lim.ip_daily_files} 个文件，请登录后上传或稍后再试")
    if used + incoming > lim.ip_daily_bytes:
        raise Rejected(429, "ip_daily_bytes",
                       f"同一网络 24 小时内最多匿名上传 {storage_size(lim.ip_daily_bytes)}，"
                       f"剩余 {storage_size(max(0, lim.ip_daily_bytes - used))}")
    if await _hourly_files(db) >= lim.hourly_files:
        raise Rejected(429, "hourly_files", "匿名上传人数较多，请稍后再试或登录后上传")
    if stored_bytes is None:
        owner = await db.get(User, await system_user_id(db))
        stored_bytes = owner.used_bytes if owner else 0
    if stored_bytes + incoming > lim.total_bytes:
        raise Rejected(507, "total_bytes", "匿名上传空间已满，请稍后再试或登录后上传")
    return max(0, min(lim.max_bytes, lim.ip_daily_bytes - used, lim.total_bytes - stored_bytes))


def storage_size(value: int) -> str:
    from ..core.templating import human_size
    return human_size(value)


# ------------------------------------------------------------------ 入库


async def save(
    db: AsyncSession, request: Request, *, key: str, stored: storage.StoredObject,
    filename: str, expires_hours: int, lim: Limits,
) -> FileItem:
    """在一个事务里复核限额、写文件记录、写成功日志；任何一步失败都整体回滚。"""
    owner_id = await system_user_id(db)
    # sync_usage 先写后读，取得写锁：之后的统计不会和其他 worker 的匿名上传交错
    _, stored_bytes = await quota.sync_usage(db, owner_id)
    await check(db, key, stored.size, lim, stored_bytes=stored_bytes)
    name = storage.safe_filename(filename)
    ip = client_ip(request)
    item = FileItem(
        public_id=new_token(8), user_id=owner_id, original_name=name, stored_path=stored.rel_path,
        size=stored.size, mime_type=storage.guess_mime(name), sha256=stored.sha256,
        is_public=True, uploaded_ip=ip, expires_at=utcnow() + timedelta(hours=expires_hours),
    )
    db.add(item)
    await db.flush()
    db.add(build_record(
        action=Action.ANON_UPLOAD, request=request, username=LOG_USERNAME, ip=key,
        target_type="file", target_id=item.id,
        detail={"filename": name, "size": stored.size, "sha256": stored.sha256, "mime_type": item.mime_type,
                "public_id": item.public_id, "expires_hours": expires_hours, "ip": ip,
                "referer": (request.headers.get("referer") or "")[:256] or None},
    ))
    await db.execute(update(User).where(User.id == owner_id).values(
        used_files=User.used_files + 1, used_bytes=User.used_bytes + stored.size))
    await db.commit()
    return item


# ------------------------------------------------------------------ 拒绝日志

#: (网段, 原因) -> [最近一次写日志的时间, 之后被省略的次数]
_recent: dict[tuple[str, str], list[float]] = {}
_DEDUP_SECONDS = 60.0


async def log_rejection(request: Request, key: str, reason: str, message: str, **detail) -> None:
    now = time.monotonic()
    slot = _recent.get((key, reason))
    if slot is not None and now - slot[0] < _DEDUP_SECONDS:
        slot[1] += 1
        return
    suppressed = int(slot[1]) if slot else 0
    _recent[(key, reason)] = [now, 0]
    if len(_recent) > 10000:
        for old in [k for k, (at, _) in _recent.items() if now - at >= _DEDUP_SECONDS]:
            _recent.pop(old, None)
    payload = {"reason": reason, "message": message, "ip": client_ip(request),
               "filename": request.query_params.get("filename"),
               "declared_size": request.headers.get("content-length"),
               "user_agent": user_agent(request) or None, **detail}
    if suppressed:
        payload["suppressed"] = suppressed
    await log_activity(action=Action.ANON_UPLOAD_REJECTED, request=request, status="failure",
                       username=LOG_USERNAME, ip=key, target_type="file",
                       detail={k: v for k, v in payload.items() if v is not None})


def reset_for_tests() -> None:
    global _system_user_id
    _system_user_id = None
    _recent.clear()
    request_limiter._hits.clear()


__all__ = ["ANONYMOUS_USERNAME", "Limits", "Rejected", "check", "limit_key", "limits", "log_rejection",
           "request_limiter", "save", "system_user_id"]
