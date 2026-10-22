"""One OS-lock elected coordinator; durable user intent always wins over engine state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import func, or_, select, update

from ..core.config import settings
from ..core.database import SessionLocal
from ..models import FileItem, OfflineTask, OfflineTaskFile, OfflineTransfer, User, utcnow
from . import files as files_service
from . import offline, offline_storage as disk, quota, settings_service, storage
from .activity import log_activity
from .aria2_rpc import Aria2Error, Aria2ResponseTooLarge, rpc
from .upload_slots import _lock

logger = logging.getLogger(__name__)
_runner: asyncio.Task | None = None
_imports: dict[str, tuple[asyncio.Task, threading.Event]] = {}
_applied: tuple[str, str] | None = None
_seed_applied: dict[str, str] = {}
_STATUS_MAX_AGE = 20


async def service_status(db=None) -> dict:
    try:
        value = json.loads((settings.offline_root / "control" / "service.json").read_text(encoding="utf-8"))
        if time.time() - float(value.get("updated_at", 0)) <= _STATUS_MAX_AGE:
            if db is not None:
                value["enabled"] = await settings_service.get_bool(db, "offline.enabled", False)
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {"available": False, "config_synced": False, "error": "下载服务正在连接或尚未启动",
            "enabled": await settings_service.get_bool(db, "offline.enabled", False) if db is not None else False}


def _seed_options(values: dict) -> dict[str, str]:
    return {"seed-time": str(values["offline.seed_minutes"]) if values["offline.seed_enabled"] else "0",
            "seed-ratio": str(values["offline.seed_ratio"])}


def _global_options(values: dict) -> dict[str, str]:
    return {"max-overall-download-limit": str(int(float(values["offline.download_limit_mbps"]) * 1024**2)),
            "max-overall-upload-limit": str(int(float(values["offline.upload_limit_mbps"]) * 1024**2)),
            "max-concurrent-downloads": str(values["offline.global_concurrency"]), **_seed_options(values)}


async def _set_task(task_id: str, **values) -> None:
    async with SessionLocal() as db:
        # State updates may not replace a concurrently changed cancel/pause intent.
        await db.execute(update(OfflineTask).where(OfflineTask.id == task_id).values(**values))
        await db.commit()


async def _snapshot(task_id: str):
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        if task is None:
            return None
        owner = await db.get(User, task.user_id)
        transfers = list((await db.scalars(select(OfflineTransfer).where(OfflineTransfer.task_id == task_id).order_by(OfflineTransfer.id))).all())
        files = list((await db.scalars(select(OfflineTaskFile).where(OfflineTaskFile.task_id == task_id).order_by(OfflineTaskFile.file_index))).all())
        return task, owner, transfers, files


async def _tell(gid: str, *, detailed: bool = False):
    try:
        return await rpc.status(gid, detailed=detailed)
    except Aria2Error as exc:
        if exc.code is not None:  # A validated GID no longer exists in this engine session.
            return None
        raise


async def _register_children(task: OfflineTask, transfers: list[OfflineTransfer]) -> None:
    async with SessionLocal() as db:
        known = set((await db.scalars(select(OfflineTransfer.gid).where(OfflineTransfer.task_id == task.id))).all())
    for transfer in list(transfers):
        status = await _tell(transfer.gid)
        if not status:
            if transfer.state != "pending":
                async with SessionLocal() as db:
                    await db.execute(update(OfflineTransfer).where(OfflineTransfer.id == transfer.id).values(state="removed"))
                    await db.commit()
            continue
        children = status.get("followedBy", [])
        if children:
            transfer.role = "metadata"
        async with SessionLocal() as db:
            await db.execute(update(OfflineTransfer).where(OfflineTransfer.id == transfer.id).values(
                state="stopping" if status["status"] == "removed" else status["status"], role=transfer.role))
            for gid in children:
                if isinstance(gid, str) and re.fullmatch(r"[a-fA-F0-9]{16}", gid) and gid not in known:
                    db.add(OfflineTransfer(task_id=task.id, gid=gid, parent_gid=transfer.gid, role="payload", state="paused"))
                    known.add(gid)
            await db.commit()


async def _stop_transfers(task_id: str) -> None:
    snapshot = await _snapshot(task_id)
    if not snapshot:
        return
    task, _, transfers, _ = snapshot
    if not any(t.state != "removed" for t in transfers):
        return  # Never-submitted tasks and already-cleaned history are local operations.
    stopped = set()
    while True:
        snapshot = await _snapshot(task_id)
        pending = [t for t in snapshot[2] if t.state != "removed" and t.id not in stopped]
        if not pending:
            break
        await _register_children(task, pending)
        for transfer in pending:
            status = await _tell(transfer.gid)
            if status and status["status"] in {"active", "waiting", "paused"}:
                try:
                    await rpc.call("forceRemove", transfer.gid)
                except Aria2Error as exc:
                    if exc.code is None:
                        raise
                after = await _tell(transfer.gid)
                if after and after["status"] in {"active", "waiting", "paused"}:
                    raise Aria2Error("任务正在停止，请稍后重试")
            # Metadata can finish and spawn a child while forceRemove is in flight.
            # Persist that child before discarding the parent's result, then stop it too.
            await _register_children(task, [transfer])
            if status:
                try:
                    await rpc.call("removeDownloadResult", transfer.gid)
                except Aria2Error as exc:
                    if exc.code is None:
                        raise
            stopped.add(transfer.id)
    await rpc.call("saveSession")
    async with SessionLocal() as db:
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.id.in_(stopped)).values(state="removed"))
        await db.commit()


async def _pause_transfer(gid: str) -> None:
    status = await _tell(gid)
    if status and status["status"] in {"active", "waiting"}:
        await rpc.call("forcePause", gid)
        status = await _tell(gid)
    async with SessionLocal() as db:
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == gid).values(
            state=status["status"] if status else "removed"))
        await db.commit()


async def _download_count(db, task: OfflineTask) -> int:
    return await db.scalar(select(func.count(func.distinct(OfflineTask.id))).select_from(OfflineTask).join(
        OfflineTransfer, OfflineTransfer.task_id == OfflineTask.id).where(
        OfflineTask.user_id == task.user_id, OfflineTask.id != task.id, OfflineTask.imported_at.is_(None),
        OfflineTransfer.state.in_(["active", "waiting", "pending"]), OfflineTask.removed_at.is_(None)))


async def _cleanup(task_id: str) -> None:
    running = _imports.get(task_id)
    if running and not running[0].done():
        running[1].set()
        return
    await _stop_transfers(task_id)
    snapshot = await _snapshot(task_id)
    if not snapshot:
        return
    task, _, _, items = snapshot
    async with SessionLocal() as db:
        for item in items:
            if item.stored_path and not item.imported_at:
                exists = await db.scalar(select(FileItem.id).where(FileItem.public_id == item.public_id))
                if exists is None:
                    if not await storage.delete_object(item.stored_path):
                        raise disk.OfflineStorageError("保存副本清理失败，等待重试")
    await asyncio.to_thread(disk.cleanup_task, task_id)
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        if not task:
            return
        offline.release_reservation(task)
        task.cleanup_pending = False
        task.download_speed = task.upload_speed = task.connections = 0
        task.state = "saved" if task.imported_at else "cancelled"
        task.seed_started_at = None
        if task.desired_state == "removed":
            task.removed_at = utcnow()
            task.source_encrypted = ""
        await db.commit()


async def cancel_user_tasks(user_id: int) -> None:
    """User deletion calls this after disabling the account, before removing any task FK."""
    async with SessionLocal() as db:
        ids = list((await db.scalars(select(OfflineTask.id).where(
            OfflineTask.user_id == user_id, OfflineTask.removed_at.is_(None),
            or_(OfflineTask.state.not_in(["saved", "cancelled"]), OfflineTask.cleanup_pending.is_(True))))).all())
        await db.execute(update(OfflineTask).where(OfflineTask.id.in_(ids)).values(
            desired_state="cancelled", cleanup_pending=True, revision=OfflineTask.revision + 1))
        await db.commit()
    for task_id in ids:
        await _cleanup(task_id)
        snapshot = await _snapshot(task_id)
        if snapshot and snapshot[0].cleanup_pending:
            raise Aria2Error("离线任务仍在停止，请稍后重试删除用户")


async def file_removed(db, file_id: int) -> None:
    ids = list((await db.scalars(select(OfflineTaskFile.task_id).where(OfflineTaskFile.file_id == file_id))).all())
    if ids:
        await db.execute(update(OfflineTask).where(OfflineTask.id.in_(ids), OfflineTask.imported_at.is_not(None)).values(
            cleanup_pending=True, seed_started_at=None, state="saved"))


async def _budget(values: dict, task: OfflineTask, *, import_bytes: int = 0) -> bool:
    minimum = int(values["offline.min_free_mb"]) * 1024**2
    maximum = int(values["offline.temp_max_mb"]) * 1024**2
    used = await asyncio.to_thread(disk.directory_bytes, settings.offline_root / "tasks")
    async with SessionLocal() as db:
        pending = list((await db.scalars(select(OfflineTaskFile).where(
            OfflineTaskFile.stored_path.is_not(None), OfflineTaskFile.imported_at.is_(None)))).all())
    for item in pending:
        target = storage.resolve(item.stored_path)
        try:
            used += target.stat().st_size
        except FileNotFoundError:
            pass
    used += await asyncio.to_thread(lambda: sum(p.stat().st_size for p in settings.tmp_dir.glob("offline-import-*.part") if p.exists()))
    if used + import_bytes > maximum:
        return False
    for root in (settings.offline_root, settings.data_dir):
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < minimum + import_bytes + 64 * 1024**2:
            return False
    return True


async def _import(task_id: str, stop: threading.Event) -> None:
    snapshots = await _snapshot(task_id)
    if not snapshots:
        return
    task, owner, _, items = snapshots
    selected = [item for item in items if item.selected and not item.imported_at]
    if task.imported_at or not selected or task.desired_state != "running" or not owner or not owner.is_active:
        return
    # Persist every destination identity before the first filesystem write.
    async with SessionLocal() as db:
        for item in selected:
            current = await db.get(OfflineTaskFile, item.id)
            current.public_id = current.public_id or uuid4().hex[:16]
            current.stored_path = current.stored_path or storage.make_rel_path()
            item.public_id, item.stored_path = current.public_id, current.stored_path
        await db.commit()
    await _set_task(task_id, state="saving", error=None)
    copies = []
    try:
        for item in selected:
            copying = asyncio.create_task(asyncio.to_thread(disk.copy_import, task_id, item.file_index,
                item.relative_path, item.stored_path, item.size, stop))
            try:
                stored = await asyncio.shield(copying)
            except asyncio.CancelledError:
                stop.set()
                await copying
                raise
            copies.append((item, stored))
        async with SessionLocal() as db:
            # First write obtains SQLite serialization before reading user intent.
            await quota.sync_usage(db, task.user_id)
            current = await db.get(OfflineTask, task_id, populate_existing=True)
            owner = await db.get(User, task.user_id, populate_existing=True)
            if stop.is_set() or not current or current.desired_state != "running" or current.imported_at or not owner or not owner.is_active:
                await db.rollback()
                return
            site_limit = await settings_service.get_int(db, "quota.max_upload_mb", settings.max_upload_mb) * 1024**2
            if any(stored.size > site_limit for _, stored in copies):
                raise offline.OfflineError("文件超过当前站点单文件上限")
            await quota.consume_many(db, task.user_id, [s.size for _, s in copies], exclude_task_id=task_id)
            expires = await files_service.resolve_expiry(db, current.expires_hours, allow_forever=owner.can_save_forever)
            imported_at = utcnow()
            for item, stored in copies:
                record = FileItem(user_id=task.user_id, public_id=item.public_id, original_name=storage.safe_filename(item.name),
                    stored_path=stored.rel_path, size=stored.size, mime_type=storage.guess_mime(item.name),
                    sha256=stored.sha256, is_public=current.is_public, expires_at=expires)
                db.add(record)
                await db.flush()
                mapped = await db.get(OfflineTaskFile, item.id)
                mapped.file_id, mapped.imported_at = record.id, imported_at
            current.imported_at = imported_at
            current.state = "saved"
            current.error = None
            current.completed_bytes = current.total_bytes = sum(s.size for _, s in copies)
            current.download_speed = 0
            current.cleanup_pending = True  # Reconciled into seeding or final source cleanup next tick.
            offline.release_reservation(current)
            await db.commit()
        await log_activity(action="offline_saved", user=owner, target_type="offline_task", target_id=task_id,
                           detail={"files": len(copies), "size": sum(s.size for _, s in copies), "is_public": task.is_public})
    except (quota.QuotaExceeded, offline.OfflineError, files_service.ForeverNotAllowed) as exc:
        await _set_task(task_id, state="quota_wait", error=str(exc)[:300] or "保存权限已变更")
    except (OSError, disk.OfflineStorageError):
        await _set_task(task_id, state="error", error="文件保存失败，可在释放空间或检查下载文件后重试")
    finally:
        # Prepared copies have durable identities, but no public file until the
        # atomic DB commit. Recover or remove them without touching referenced blobs.
        async with SessionLocal() as db:
            for item in selected:
                if item.stored_path:
                    exists = await db.scalar(select(FileItem.id).where(FileItem.public_id == item.public_id))
                    if exists is None:
                        await storage.delete_object(item.stored_path)


async def _ensure_engine(task: OfflineTask, files: list[OfflineTaskFile], values: dict) -> OfflineTransfer | None:
    async with SessionLocal() as db:
        previous = list((await db.scalars(select(OfflineTransfer).where(OfflineTransfer.task_id == task.id).order_by(OfflineTransfer.id))).all())
        current = previous[-1] if previous else None
        if current and current.state != "removed" and await _tell(current.gid):
            return current
        if task.info_hash:
            other = await db.scalar(select(OfflineTransfer.id).join(OfflineTask, OfflineTransfer.task_id == OfflineTask.id).where(
                OfflineTask.info_hash == task.info_hash, OfflineTask.id != task.id,
                OfflineTransfer.state.not_in(["removed", "error"]), OfflineTask.removed_at.is_(None)))
            if other:
                await db.execute(update(OfflineTask).where(OfflineTask.id == task.id).values(
                    state="resource_wait", error="相同资源排队中"))
                await db.commit()
                return None
        if await _download_count(db, task) >= int(values["offline.user_concurrency"]):
            return None
        role = "metadata" if task.kind == "magnet" else "payload"
        # Only an unacknowledged submission reuses a GID. A lost magnet payload
        # restarts as metadata, never as a payload-role magnet entry.
        if current and current.state == "pending" and current.role == role:
            transfer = current
        else:
            transfer = OfflineTransfer(task_id=task.id, gid=uuid4().hex[:16],
                role=role, state="pending")
            db.add(transfer)
            await db.commit()
    await asyncio.to_thread(disk.ensure_task_dir, task.id)
    options = {"gid": transfer.gid, "dir": str(PurePosixPath(settings.aria2_download_dir) / "tasks" / task.id / "payload"),
               "pause": "true", "pause-metadata": "true", "continue": "true", "file-allocation": "none",
               "allow-overwrite": "false", "auto-file-renaming": "false", "follow-metalink": "false",
               "follow-torrent": "false", "check-integrity": "true", **_seed_options(values)}
    source = settings_service.decrypt_secret(task.source_encrypted)
    if task.kind == "torrent":
        options["select-file"] = ",".join(str(f.file_index) for f in files if f.selected)
        await rpc.call("addTorrent", source, [], options)
    else:
        if task.kind == "magnet":
            options["follow-torrent"] = "true"
            options["bt-save-metadata"] = "true"
            if files:
                options["select-file"] = ",".join(str(f.file_index) for f in files if f.selected)
        await rpc.call("addUri", [source], options)
    await rpc.call("saveSession")
    return transfer


async def _update_files(task: OfflineTask, status: dict, values: dict) -> list[OfflineTaskFile]:
    engine_files = status.get("files", [])
    if len(engine_files) > int(values["offline.max_torrent_files"]):
        raise offline.OfflineError("下载文件数量超过站点上限")
    async with SessionLocal() as db:
        await db.execute(update(OfflineTask).where(OfflineTask.id == task.id).values(id=task.id))
        current = await db.get(OfflineTask, task.id)
        existing = {f.file_index: f for f in (await db.scalars(select(OfflineTaskFile).where(OfflineTaskFile.task_id == task.id))).all()}
        for raw in engine_files:
            index = int(raw["index"])
            # A paused HTTP entry has no filename until the first response headers.
            # Keep its conservative reservation while it starts resolving the file.
            if task.kind == "http" and not raw.get("path") and status["status"] != "complete" and int(raw["length"]) == 0 and int(raw.get("completedLength", 0)) == 0:
                continue
            relative = disk.relative_engine_path(task.id, raw["path"])
            length = int(raw["length"])
            if not 0 <= length < 2**63:
                raise offline.OfflineError("下载引擎返回了非法文件大小")
            item = existing.get(index)
            if not item:
                item = OfflineTaskFile(task_id=task.id, file_index=index, selected=True, name="", relative_path="", size=0)
                db.add(item)
                existing[index] = item
            if not item.imported_at:
                item.relative_path, item.name, item.size = relative, storage.safe_filename(PurePosixPath(relative).name), length
                completed = int(raw.get("completedLength", 0))
                if not 0 <= completed <= length:
                    raise offline.OfflineError("下载引擎返回了非法文件进度")
                item.completed_bytes = completed
        if sum(f.size for f in existing.values()) >= 2**63:
            raise offline.OfflineError("资源总大小超过支持范围")
        if task.kind != "http" and not current.imported_at and current.reserved_files == 0:
            current.state = "awaiting_selection"
            current.name = storage.safe_filename(status["bittorrent"].get("info", {}).get("name", current.name))
            current.total_bytes = sum(f.size for f in existing.values())
        await db.commit()
        return list(existing.values())


async def _admit_transfer(task: OfflineTask, gid: str, values: dict) -> bool:
    """The elected coordinator reserves a user slot before every actual unpause."""
    async with SessionLocal() as db:
        await db.execute(update(OfflineTask).where(OfflineTask.id == task.id).values(id=task.id))
        current = await db.get(OfflineTask, task.id)
        if not current or current.desired_state != "running":
            await db.rollback()
            return False
        count = await _download_count(db, task)
        if count >= int(values["offline.user_concurrency"]):
            return False
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == gid).values(state="active"))
        await db.commit()
        return True


async def _handle(task_id: str, values: dict, watchdog_blocked: bool) -> None:
    snapshot = await _snapshot(task_id)
    if not snapshot:
        return
    task, owner, transfers, items = snapshot
    if not owner or not owner.is_active:
        await _set_task(task_id, desired_state="cancelled", cleanup_pending=True)
        task.desired_state = "cancelled"
    if task.desired_state in {"cancelled", "removed"}:
        await _cleanup(task_id)
        return
    if task_id in _imports:
        return
    if task.payload_ready and not task.imported_at and task.state != "error" and task.desired_state == "running" and values["offline.enabled"]:
        await _schedule_import(task, values)
        return
    if task.imported_at and task.cleanup_pending and not task.seed_started_at:
        # Cleanup after saving can become seeding only while all saved files still exist.
        can_seed = bool(values["offline.seed_enabled"]) and task.kind != "http"
        async with SessionLocal() as db:
            for f in items:
                record = await db.get(FileItem, f.file_id) if f.file_id else None
                if f.selected and (not record or record.is_deleted or record.is_expired):
                    can_seed = False
        if can_seed:
            task.seed_started_at = utcnow()
            await _set_task(task_id, state="seeding", cleanup_pending=False, seed_started_at=task.seed_started_at)
        else:
            await _cleanup(task_id)
            return
    if task.imported_at and task.seed_started_at and not values["offline.seed_enabled"]:
        await _cleanup(task_id)
        return
    if task.state == "error":
        await _stop_transfers(task_id)
        return
    if task.kind == "torrent" and task.state == "awaiting_selection" and not transfers:
        return
    if not values["offline.enabled"] or task.desired_state == "paused" or watchdog_blocked:
        for transfer in transfers:
            if transfer.state != "removed":
                await _pause_transfer(transfer.gid)
        if not task.imported_at:
            await _set_task(task_id, state="paused", download_speed=0, upload_speed=0,
                error="等待可用磁盘空间" if watchdog_blocked else None)
        return
    await _register_children(task, transfers)
    snapshot = await _snapshot(task_id)
    task, _, transfers, items = snapshot
    payloads = [t for t in transfers if t.role == "payload" and t.state != "removed"]
    transfer = payloads[-1] if payloads else (transfers[-1] if transfers else None)
    if transfer is None or await _tell(transfer.gid) is None:
        if not await _budget(values, task):
            await _set_task(task_id, state="quota_wait", error="离线暂存空间不足")
            return
        transfer = await _ensure_engine(task, items, values)
        if transfer is None:
            return
    status = await _tell(transfer.gid, detailed=True)
    if not status:
        return
    await _register_children(task, [transfer])
    # Magnet metadata has child transfers. Process those on the next tick.
    if status.get("followedBy"):
        return
    metadata_only = transfer.role == "metadata"
    if status["status"] == "error":
        if task.state in {"queued", "resource_wait"} and transfer.state == "error":
            # Explicit retry discards the engine's failed result but preserves partial payload.
            try:
                await rpc.call("removeDownloadResult", transfer.gid)
            except Aria2Error:
                pass
            await _ensure_engine(task, items, values)
            return
        await _set_task(task_id, state="error", error="下载失败，请检查资源有效性后重试", download_speed=0, upload_speed=0)
        return
    if metadata_only:
        if status["status"] == "paused":
            if not await _admit_transfer(task, transfer.gid, values):
                return
            await rpc.call("unpause", transfer.gid)
        await _set_task(task_id, state="resolving", download_speed=int(status.get("downloadSpeed", 0)))
        return
    items = await _update_files(task, status, values)
    latest = await _snapshot(task_id)
    task = latest[0]
    if task.state == "awaiting_selection":
        if status["status"] in {"active", "waiting"}:
            await _pause_transfer(transfer.gid)
        return
    selected = [f for f in items if f.selected]
    if not selected:
        raise offline.OfflineError("任务没有可下载的文件")
    length = sum(f.size for f in selected)
    if length and not task.imported_at:
        async with SessionLocal() as db:
            await db.execute(update(OfflineTask).where(OfflineTask.id == task_id).values(id=task_id))
            current = await db.get(OfflineTask, task_id)
            if not current or current.desired_state != "running":
                await db.rollback()
                return
            try:
                await offline.reserve_task(db, current, [f.size for f in selected])
                await db.commit()
            except (offline.OfflineError, quota.QuotaExceeded) as exc:
                await db.rollback()
                if status["status"] in {"active", "waiting"}:
                    await _pause_transfer(transfer.gid)
                await _set_task(task_id, state="quota_wait", error=str(exc)[:300])
                return
    seed_fingerprint = json.dumps(_seed_options(values), sort_keys=True)
    if _seed_applied.get(transfer.gid) != seed_fingerprint and status["status"] in {"active", "paused", "waiting"}:
        await rpc.call("changeOption", transfer.gid, _seed_options(values))
        _seed_applied[transfer.gid] = seed_fingerprint
    if status["status"] == "paused":
        if not await _admit_transfer(task, transfer.gid, values):
            return
        if task.kind != "http":
            await rpc.call("changeOption", transfer.gid, {"select-file": ",".join(str(f.file_index) for f in selected)})
        await rpc.call("unpause", transfer.gid)
    complete = all(f.completed_bytes >= f.size for f in selected) and not metadata_only
    if task.kind == "http":
        complete = status["status"] == "complete"
    else:
        pending_verify = str(status.get("verifyIntegrityPending", "false")).lower() == "true"
        # Aria2 can discard per-file piece counters when a BT result stops.
        # Its aggregate counters describe the selected files and survive that step.
        selected_complete = status["status"] == "complete" or (
            int(status.get("totalLength", -1)) == length and int(status.get("completedLength", -1)) >= length)
        complete = (complete or selected_complete) and not pending_verify and "verifiedLength" not in status
    if task.imported_at:
        elapsed = (utcnow() - (task.seed_started_at or utcnow())).total_seconds()
        if status["status"] in {"complete", "removed", "error"} or elapsed >= float(values["offline.seed_minutes"]) * 60:
            await _cleanup(task_id)
        else:
            await _set_task(task_id, state="seeding", upload_speed=int(status.get("uploadSpeed", 0)),
                download_speed=0, connections=int(status.get("connections", 0)))
        return
    progress = max(sum(f.completed_bytes for f in selected), min(length, int(status.get("completedLength", 0))))
    await _set_task(task_id, total_bytes=length, completed_bytes=progress,
        download_speed=int(status.get("downloadSpeed", 0)), upload_speed=int(status.get("uploadSpeed", 0)),
        connections=int(status.get("connections", 0)), state="downloading", error=None)
    if complete:
        await _set_task(task_id, payload_ready=True)
        task.total_bytes = length
        await _schedule_import(task, values)


async def _schedule_import(task: OfflineTask, values: dict) -> None:
    if not await _budget(values, task, import_bytes=task.total_bytes):
        await _set_task(task.id, state="quota_wait", error="文件已下载，等待足够空间保存")
        return
    if not _imports:
        stop = threading.Event()
        _imports[task.id] = (asyncio.create_task(_import(task.id, stop)), stop)


async def run_once() -> None:
    global _applied
    async with SessionLocal() as db:
        values = await settings_service.all_values(db, force=True)
        tasks = list((await db.scalars(select(OfflineTask).where(
            OfflineTask.removed_at.is_(None), or_(OfflineTask.state.not_in(["saved", "cancelled"]), OfflineTask.cleanup_pending.is_(True)))
            .order_by(OfflineTask.created_at))).all())
    for task_id, (job, _) in list(_imports.items()):
        if job.done():
            try:
                job.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("离线文件保存发生内部错误（任务 %s）", task_id)
                await _set_task(task_id, state="error", error="保存失败，可稍后重试")
            del _imports[task_id]
    policy = {"temp_max_bytes": int(values["offline.temp_max_mb"]) * 1024**2,
              "min_free_bytes": int(values["offline.min_free_mb"]) * 1024**2,
              "tasks": {t.id: {"max_bytes": max(t.reserved_bytes, t.total_bytes, int(values["offline.metadata_max_mb"]) * 1024**2) + 32 * 1024**2,
                               "desired_state": t.desired_state if values["offline.enabled"] and t.state != "error" else "paused"} for t in tasks}}
    await asyncio.to_thread(disk.write_control, "policy.json", policy)
    # Purely local cancellation does not depend on an engine connection.
    for task in list(tasks):
        if task.desired_state in {"cancelled", "removed"}:
            snapshot = await _snapshot(task.id)
            if snapshot and all(t.state == "removed" for t in snapshot[2]):
                await _cleanup(task.id)
                tasks.remove(task)
        elif task.payload_ready and not task.imported_at and task.desired_state == "running" and task.state != "error" and values["offline.enabled"]:
            await _schedule_import(task, values)
    try:
        session = await rpc.call("getSessionInfo")
        fingerprint = json.dumps(_global_options(values), sort_keys=True)
        engine_id = str(session.get("sessionId", ""))
        if _applied != (engine_id, fingerprint):
            await rpc.call("changeGlobalOption", _global_options(values))
            _applied = (engine_id, fingerprint)
            _seed_applied.clear()
        blocked = False
        try:
            watchdog = json.loads((settings.offline_root / "control" / "watchdog-status.json").read_text())
            blocked = bool(watchdog.get("blocked"))
        except (OSError, ValueError):
            pass
        for task in tasks:
            try:
                await _handle(task.id, values, blocked)
            except Aria2Error as exc:
                if exc.code is None and not isinstance(exc, Aria2ResponseTooLarge):
                    raise
                message = str(exc) if isinstance(exc, Aria2ResponseTooLarge) else "下载引擎拒绝此任务，请检查资源后重试"
                await _set_task(task.id, state="error", error=message,
                                download_speed=0, upload_speed=0)
                await _stop_transfers(task.id)
            except (offline.OfflineError, disk.OfflineStorageError, ValueError, KeyError) as exc:
                await _set_task(task.id, state="error", error=str(exc)[:200] if isinstance(exc, offline.OfflineError) else "任务文件或元数据无效")
                # Stop untrusted payload on validation failure, but keep intent for an explicit retry.
                await _stop_transfers(task.id)
        status = {"available": True, "config_synced": True, "error": None, "updated_at": time.time()}
    except Aria2Error as exc:
        status = {"available": False, "config_synced": False, "error": str(exc), "updated_at": time.time()}
    await asyncio.to_thread(disk.write_control, "service.json", status)


async def _run() -> None:
    handle = None
    try:
        while True:
            if handle is None:
                handle = _lock(settings.data_dir / ".offline-worker.lock")
            if handle is not None:
                try:
                    await run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("离线任务协调失败")
            await asyncio.sleep(max(0.5, settings.offline_poll_seconds))
    finally:
        for job, stop in _imports.values():
            stop.set()
            job.cancel()
        await asyncio.gather(*(job for job, _ in _imports.values()), return_exceptions=True)
        _imports.clear()
        if handle:
            handle.close()


def start() -> None:
    global _runner
    _runner = asyncio.create_task(_run())


async def stop() -> None:
    global _runner
    if _runner:
        _runner.cancel()
        await asyncio.gather(_runner, return_exceptions=True)
        _runner = None
