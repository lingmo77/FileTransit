"""Authenticated offline task intent and quota reservations; no public RPC passthrough."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.templating import fmt_iso
from ..models import OfflineTask, OfflineTaskFile, OfflineTransfer, User, utcnow
from . import quota, settings_service, storage
from .offline_network import is_site_url, site_hostname
from .torrent_metadata import TorrentError, parse_torrent

TERMINAL_STATES = {"saved", "cancelled"}
RUNNING_STATES = {"queued", "resolving", "awaiting_selection", "resource_wait", "downloading", "paused", "saving", "quota_wait", "cancelling"}


class OfflineError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def ensure_enabled(db: AsyncSession) -> None:
    if not await settings_service.get_bool(db, "offline.enabled", False):
        raise OfflineError("管理员尚未开启离线下载", 503)


async def resolve_hours(db: AsyncSession, user: User, hours: int | None = None) -> int:
    maximum = max(1, await settings_service.get_int(db, "quota.max_expire_days", 30)) * 24
    if hours is None:
        return min(336, maximum)
    if hours == 0 and user.can_save_forever:
        return 0
    if not 1 <= hours <= maximum:
        raise OfflineError(f"保留时间必须在 1 小时至 {maximum // 24} 天之间")
    return hours


async def validate_http_url(value: str) -> str:
    """Initial check supplements, but never replaces, the downloader's network egress filter."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OfflineError("下载链接不合法") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OfflineError("仅支持不含账号密码的 HTTP/HTTPS 文件链接")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096:
        raise OfflineError("下载链接过长或包含非法字符")
    host = parsed.hostname.rstrip(".")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise OfflineError("不能下载本地或内网地址")
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM),
            timeout=5,
        )
    except (OSError, TimeoutError) as exc:
        raise OfflineError("无法解析下载地址，请检查链接") from exc
    if not addresses:
        raise OfflineError("无法解析下载地址")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise OfflineError("下载地址解析结果不合法") from exc
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise OfflineError("不能下载本地、内网或保留地址")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def normalize_magnet(value: str) -> tuple[str, str, str]:
    if len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise OfflineError("磁力链接过长或包含非法字符")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise OfflineError("磁力链接不合法") from exc
    if parsed.scheme.lower() != "magnet":
        raise OfflineError("磁力链接不合法")
    try:
        params = parse_qs(parsed.query, max_num_fields=100)
    except ValueError as exc:
        raise OfflineError("磁力链接参数过多") from exc
    hashes = [v[9:] for v in params.get("xt", []) if v.lower().startswith("urn:btih:")]
    if len(hashes) != 1:
        raise OfflineError("需要包含一个 BT v1 infohash 的磁力链接")
    digest = hashes[0]
    if re.fullmatch(r"[0-9a-fA-F]{40}", digest):
        digest = digest.lower()
    elif re.fullmatch(r"[A-Za-z2-7]{32}", digest):
        digest = base64.b32decode(digest.upper()).hex()
    else:
        raise OfflineError("磁力链接 infohash 不合法")
    name = storage.safe_filename(params.get("dn", ["磁力任务"])[0])
    normalized = [("xt", "urn:btih:" + digest)]
    if params.get("dn"):
        normalized.append(("dn", name))
    trackers = params.get("tr", [])
    if len(trackers) > 30:
        raise OfflineError("磁力链接 Tracker 数量过多")
    for tracker in trackers:
        try:
            tr = urlsplit(tracker)
            _ = tr.port
        except ValueError as exc:
            raise OfflineError("磁力 Tracker 地址不合法") from exc
        if tr.scheme.lower() not in {"http", "https", "udp"} or not tr.hostname or tr.username or tr.password or len(tracker) > 2048:
            raise OfflineError("磁力 Tracker 地址不合法")
        try:
            address = ipaddress.ip_address(tr.hostname)
        except ValueError:
            if tr.hostname.lower().rstrip(".") == "localhost":
                raise OfflineError("不允许本地 Tracker")
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise OfflineError("不允许内网或保留地址 Tracker")
        normalized.append(("tr", tracker))
    return "magnet:?" + urlencode(normalized), digest, name


async def get_owned_task(db: AsyncSession, user: User, task_id: str, *, include_removed: bool = False) -> OfflineTask:
    stmt = select(OfflineTask).where(OfflineTask.id == task_id)
    if not user.is_admin:
        stmt = stmt.where(OfflineTask.user_id == user.id)
    if not include_removed:
        stmt = stmt.where(OfflineTask.removed_at.is_(None))
    task = await db.scalar(stmt)
    if task is None:
        raise OfflineError("任务不存在", 404)
    return task


async def _lock_user(db: AsyncSession, user_id: int) -> User:
    await quota.sync_usage(db, user_id)
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise OfflineError("用户不存在或已被停用", 403)
    return user


async def reserve_task(db: AsyncSession, task: OfflineTask, sizes: list[int]) -> None:
    """Caller commits; serialize against uploads and convert only this task's reservation."""
    if not sizes or any(s < 0 for s in sizes):
        raise OfflineError("请至少选择一个有效文件")
    user = await _lock_user(db, task.user_id)
    site_limit = await settings_service.get_int(db, "quota.max_upload_mb", settings.max_upload_mb) * 1024**2
    if any(size > site_limit for size in sizes):
        raise OfflineError("所选文件超过站点单文件大小上限")
    await quota.ensure_can_upload(db, user, sum(sizes), incoming_files=len(sizes), exclude_task_id=task.id)
    task.reserved_files = len(sizes)
    task.reserved_bytes = sum(sizes)


def release_reservation(task: OfflineTask) -> None:
    task.reserved_files = 0
    task.reserved_bytes = 0


async def _new_task(db: AsyncSession, user: User, *, kind: str, source: str, name: str,
                    info_hash: str | None, hours: int | None, public: bool, request_key: str | None) -> OfflineTask:
    user = await _lock_user(db, user.id)
    if request_key:
        existing = await db.scalar(select(OfflineTask).where(OfflineTask.user_id == user.id, OfflineTask.request_key == request_key))
        if existing:
            if existing.kind != kind or settings_service.decrypt_secret(existing.source_encrypted) != source:
                raise OfflineError("请求标识已用于其他任务，请刷新后重试", 409)
            return existing
    cap = max(1, await settings_service.get_int(db, "offline.max_tasks_per_user", 20))
    count = await db.scalar(select(func.count()).select_from(OfflineTask).where(
        OfflineTask.user_id == user.id, OfflineTask.removed_at.is_(None),
        OfflineTask.state.not_in(["saved", "cancelled"])))
    if count >= cap:
        raise OfflineError(f"未结束离线任务已达到上限（{cap} 个）", 429)
    task = OfflineTask(id=uuid4().hex, user_id=user.id, request_key=request_key,
                       kind=kind, source_encrypted=settings_service.encrypt_secret(source),
                       name=storage.safe_filename(name), info_hash=info_hash,
                       state="queued" if kind == "http" else ("resolving" if kind == "magnet" else "awaiting_selection"),
                       desired_state="running", is_public=public, expires_hours=await resolve_hours(db, user, hours),
                       reserved_files=0, reserved_bytes=0, total_bytes=0, completed_bytes=0,
                       download_speed=0, upload_speed=0, connections=0, cleanup_pending=False, revision=0)
    db.add(task)
    await db.flush()
    return task


async def create_link_task(db: AsyncSession, user: User, value: str, *, expires_hours: int | None = None,
                           is_public: bool = False, request_key: str | None = None) -> OfflineTask:
    await ensure_enabled(db)
    value = value.strip()
    if value.lower().startswith("magnet:"):
        source, digest, name = normalize_magnet(value)
        task = await _new_task(db, user, kind="magnet", source=source, name=name,
                               info_hash=digest, hours=expires_hours, public=is_public, request_key=request_key)
    else:
        configured = site_hostname(await settings_service.get_str(db, "site.base_url", ""))
        if is_site_url(value, configured):
            raise OfflineError("不能下载本站域名或其子域名的资源")
        source = await validate_http_url(value)
        name = storage.safe_filename(unquote(urlsplit(source).path.rsplit("/", 1)[-1]) or "等待解析文件名")
        task = await _new_task(db, user, kind="http", source=source, name=name,
                               info_hash=None, hours=expires_hours, public=is_public, request_key=request_key)
        files = await db.scalar(select(func.count()).select_from(OfflineTaskFile).where(OfflineTaskFile.task_id == task.id))
        if not files:
            owner = await db.get(User, task.user_id, populate_existing=True)
            available = await quota.resolve_quota(db, owner, exclude_task_id=task.id)
            site_limit = await settings_service.get_int(db, "quota.max_upload_mb", settings.max_upload_mb) * 1024**2
            budget = site_limit if available.remaining_bytes is None else min(site_limit, available.remaining_bytes)
            if budget <= 0:
                raise quota.QuotaExceeded("存储空间不足，无法创建离线下载")
            await reserve_task(db, task, [budget])
            db.add(OfflineTaskFile(task_id=task.id, file_index=1, relative_path="payload", name=name,
                                   size=0, selected=True, completed_bytes=0))
    await db.commit()
    return task


async def create_torrent_task(db: AsyncSession, user: User, data: bytes, *, expires_hours: int | None = None,
                              is_public: bool = False, request_key: str | None = None) -> OfflineTask:
    await ensure_enabled(db)
    try:
        metadata = parse_torrent(data,
            max_bytes=max(1, await settings_service.get_int(db, "offline.metadata_max_mb", 5)) * 1024**2,
            max_files=max(1, await settings_service.get_int(db, "offline.max_torrent_files", 1000)))
    except TorrentError as exc:
        raise OfflineError(str(exc)) from exc
    task = await _new_task(db, user, kind="torrent", source=base64.b64encode(data).decode("ascii"),
                           name=metadata.name, info_hash=metadata.info_hash, hours=expires_hours,
                           public=is_public, request_key=request_key)
    existing = await db.scalar(select(func.count()).select_from(OfflineTaskFile).where(OfflineTaskFile.task_id == task.id))
    if not existing:
        task.total_bytes = sum(f.size for f in metadata.files)
        for f in metadata.files:
            db.add(OfflineTaskFile(task_id=task.id, file_index=f.index, relative_path=f.relative_path,
                                   name=storage.safe_filename(f.name), size=f.size, selected=True, completed_bytes=0))
    await db.commit()
    return task


async def select_files(db: AsyncSession, user: User, task_id: str, indexes: list[int]) -> OfflineTask:
    await ensure_enabled(db)
    task = await get_owned_task(db, user, task_id)
    await _lock_user(db, task.user_id)
    await db.refresh(task)
    if task.kind == "http" or task.state not in {"awaiting_selection", "quota_wait"} or task.imported_at:
        raise OfflineError("当前任务不能修改文件选择", 409)
    files = (await db.scalars(select(OfflineTaskFile).where(OfflineTaskFile.task_id == task.id))).all()
    wanted = set(indexes)
    known = {f.file_index for f in files}
    if not wanted or wanted - known:
        raise OfflineError("请选择至少一个有效文件")
    await reserve_task(db, task, [f.size for f in files if f.file_index in wanted])
    for f in files:
        f.selected = f.file_index in wanted
    task.total_bytes = sum(f.size for f in files if f.selected)
    task.desired_state = "running"
    task.state = "queued"
    task.error = None
    task.revision += 1
    await db.commit()
    return task


async def action_task(db: AsyncSession, user: User, task_id: str, action: str) -> OfflineTask:
    task = await get_owned_task(db, user, task_id)
    await _lock_user(db, task.user_id)
    await db.refresh(task)
    if action not in {"pause", "resume", "cancel", "retry", "remove"}:
        raise OfflineError("不支持的任务操作")
    if action in {"resume", "retry"}:
        await ensure_enabled(db)
    if action == "pause":
        if task.imported_at or task.state in {"cancelled", "cancelling", "saving", "awaiting_selection"}:
            raise OfflineError("当前任务不能暂停", 409)
        task.desired_state = "paused"
    elif action == "resume":
        if task.imported_at or task.desired_state in {"cancelled", "removed"}:
            raise OfflineError("当前任务不能继续", 409)
        if task.state == "awaiting_selection":
            raise OfflineError("请先确认文件选择", 409)
        task.desired_state = "running"
        task.state = "queued" if task.state == "paused" else task.state
        task.error = None
    elif action == "retry":
        if task.state not in {"error", "quota_wait"} or task.desired_state in {"cancelled", "removed"}:
            raise OfflineError("当前任务无需重试", 409)
        task.desired_state = "running"
        task.state = "queued"
        task.error = None
    elif action == "cancel":
        if task.state == "saved" and not task.seed_started_at:
            raise OfflineError("任务已保存，请使用移除记录", 409)
        task.desired_state = "cancelled"
        task.state = "cancelling"
        task.cleanup_pending = True
    else:
        if not task.imported_at and task.state not in {"cancelled", "error"}:
            raise OfflineError("请先取消未完成的任务", 409)
        task.desired_state = "removed"
        task.cleanup_pending = True
    task.revision += 1
    task.updated_at = utcnow()
    await db.commit()
    return task


async def serialize_task(db: AsyncSession, task: OfflineTask) -> dict:
    files = (await db.scalars(select(OfflineTaskFile).where(OfflineTaskFile.task_id == task.id).order_by(OfflineTaskFile.file_index))).all()
    transfers = (await db.scalars(select(OfflineTransfer).where(OfflineTransfer.task_id == task.id))).all()
    return {
        "id": task.id, "kind": task.kind, "name": task.name, "state": task.state,
        "desired_state": task.desired_state, "is_public": task.is_public, "expires_hours": task.expires_hours,
        "total_bytes": task.total_bytes, "completed_bytes": task.completed_bytes,
        "download_speed": task.download_speed, "upload_speed": task.upload_speed,
        "connections": task.connections, "error": task.error,
        "reserved_files": task.reserved_files, "reserved_bytes": task.reserved_bytes,
        "imported_at": fmt_iso(task.imported_at) or None, "seed_started_at": fmt_iso(task.seed_started_at) or None,
        "created_at": fmt_iso(task.created_at), "updated_at": fmt_iso(task.updated_at),
        "files": [{"index": f.file_index, "relative_path": f.relative_path, "name": f.name,
                   "size": f.size, "selected": f.selected, "completed_bytes": f.completed_bytes,
                   "file_id": f.file_id, "public_id": f.public_id,
                   "detail_url": "/f/" + f.public_id if f.file_id and f.public_id else None,
                   "imported_at": fmt_iso(f.imported_at) or None} for f in files],
        "transfers": [{"gid": tr.gid, "role": tr.role, "state": tr.state} for tr in transfers],
    }


async def list_tasks(db: AsyncSession, user: User, *, page: int = 1, state: str = "all") -> dict:
    where = [OfflineTask.user_id == user.id, OfflineTask.removed_at.is_(None)]
    if state == "running":
        where.append(OfflineTask.state.in_(RUNNING_STATES))
    elif state == "complete":
        where.append(OfflineTask.imported_at.is_not(None))
    elif state == "error":
        where.append(OfflineTask.state == "error")
    elif state != "all":
        raise OfflineError("不支持的任务筛选")
    total = await db.scalar(select(func.count()).select_from(OfflineTask).where(*where))
    pages = max(1, (int(total) + 19) // 20)
    page = max(1, min(page, pages))
    tasks = (await db.scalars(select(OfflineTask).where(*where).order_by(OfflineTask.created_at.desc(), OfflineTask.id.desc()).offset((page - 1) * 20).limit(20))).all()
    owner = await db.get(User, user.id, populate_existing=True)
    info = await quota.resolve_quota(db, owner)
    return {"ok": True, "tasks": [await serialize_task(db, task) for task in tasks],
            "page": page, "pages": pages, "total": total,
            "enabled": await settings_service.get_bool(db, "offline.enabled", False),
            "max_expire_days": await settings_service.get_int(db, "quota.max_expire_days", 30),
            "default_expires_hours": await resolve_hours(db, owner),
            "metadata_max_mb": await settings_service.get_int(db, "offline.metadata_max_mb", 5),
            "max_torrent_files": await settings_service.get_int(db, "offline.max_torrent_files", 1000),
            "quota": {"max_files": info.max_files, "max_bytes": info.max_bytes,
                      "used_files": info.used_files, "used_bytes": info.used_bytes,
                      "reserved_files": info.reserved_files, "reserved_bytes": info.reserved_bytes,
                      "remaining_files": info.remaining_files, "remaining_bytes": info.remaining_bytes}}
