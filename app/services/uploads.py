"""Upload reservations, OS locks and expiry shared by all workers."""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select

from ..core.config import settings
from ..core.database import SessionLocal
from ..models import UploadSession, utcnow

CHUNK_BYTES = max(1, min(settings.upload_chunk_mb, 16)) * 1024**2


def deadline():
    return utcnow() + timedelta(hours=max(1, settings.upload_ttl_hours))


def part_path(upload_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(404, "上传任务不存在")
    return settings.uploads_dir / f"{upload_id}.part"


@contextmanager
def upload_lock(upload_id: str):
    """Nonblocking kernel lock: never expires while a worker is writing."""
    part_path(upload_id)
    lock = settings.uploads_dir / f"{upload_id}.lock"
    handle = open(lock, "a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if lock.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise HTTPException(409, "上传正在处理中，请稍后重试") from None
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


async def reserved(db, user_id: int, *, exclude: str | None = None):
    stmt = select(func.count(UploadSession.id), func.coalesce(func.sum(UploadSession.size), 0)).where(
        UploadSession.user_id == user_id, UploadSession.state == "pending"
    )
    if exclude:
        stmt = stmt.where(UploadSession.id != exclude)
    count, size = (await db.execute(stmt)).one()
    return int(count), int(size)


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def purge_expired_uploads():
    from . import storage
    now = utcnow()
    removed = 0
    async with SessionLocal() as db:
        ids = list((await db.execute(select(UploadSession.id).where(
            UploadSession.state == "pending", UploadSession.expires_at <= now
        ))).scalars())
    for upload_id in ids:
        try:
            with upload_lock(upload_id):
                async with SessionLocal() as db:
                    row = await db.get(UploadSession, upload_id)
                    if row is None or row.state != "pending" or row.expires_at > utcnow():
                        continue
                    part_path(upload_id).unlink(missing_ok=True)
                    # Includes recovery of a rename that happened before a failed DB commit.
                    await storage.delete_object(row.stored_path)
                    row.state = "expired"
                    row.updated_at = utcnow()
                    await db.commit()
                    removed += 1
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
    # User deletion cascades DB rows; sweep only orphaned parts, under the same lock.
    async with SessionLocal() as db:
        live = set((await db.execute(select(UploadSession.id))).scalars())
    for path in settings.uploads_dir.glob("*.part"):
        upload_id = path.stem
        if upload_id in live:
            continue
        try:
            with upload_lock(upload_id):
                async with SessionLocal() as db:
                    # A task may have been created since the initial snapshot.
                    if await db.get(UploadSession, upload_id) is None:
                        path.unlink(missing_ok=True)
        except HTTPException:
            continue
    # Keep completed receipts for seven days so a lost success response is idempotent.
    async with SessionLocal() as db:
        old = list((await db.execute(select(UploadSession).where(
            UploadSession.state != "pending",
            UploadSession.updated_at < now - timedelta(days=7),
        ))).scalars())
        for row in old:
            await db.delete(row)
        await db.commit()
    for path in settings.uploads_dir.glob("*.lock"):
        try:
            with upload_lock(path.stem):
                async with SessionLocal() as db:
                    if await db.get(UploadSession, path.stem) is None:
                        # IDs are never reused; old requests recheck the DB after locking.
                        # Windows requires closing before unlinking the lock file.
                        pass
                    else:
                        continue
            path.unlink(missing_ok=True)
        except (HTTPException, OSError):
            continue
    return removed
