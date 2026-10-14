"""Authenticated raw-body chunks; no multipart spool or full-file merge copy."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool

from ..core.database import get_db
from ..core.deps import check_csrf, require_user
from ..core.security import client_ip, upload_limiter
from ..core.templating import fmt_iso
from ..models import FileItem, UploadSession, User, utcnow
from ..services import files, quota, settings_service, storage, uploads
from ..services.activity import Action, log_activity
from .files import _serialize

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


class UploadIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_hours: int = Field(default=72, ge=0, le=24 * 365)
    is_public: bool = True


def payload(row):
    return {"id": row.id, "name": row.name, "size": row.size, "offset": row.offset,
            "state": row.state, "chunk_size": uploads.CHUNK_BYTES,
            "expires_at": fmt_iso(row.expires_at), "sha256": row.sha256,
            "expires_hours": row.expires_hours, "is_public": row.is_public}


async def owned(db, upload_id, user_id, *, pending=True):
    uploads.part_path(upload_id)
    row = await db.get(UploadSession, upload_id, populate_existing=True)
    if row is None or row.user_id != user_id:
        raise HTTPException(404, "上传任务不存在")
    if pending and (row.state != "pending" or row.expires_at <= utcnow()):
        raise HTTPException(410, "上传任务已过期或结束，请重新选择文件")
    return row


async def expiry(db, hours, user):
    try:
        return await files.resolve_expiry(db, hours, allow_forever=user.can_save_forever)
    except files.ForeverNotAllowed:
        raise HTTPException(400, "你还没有被授予永久保存的权限") from None


@router.get("")
async def list_uploads(db=Depends(get_db), auth=Depends(require_user)):
    rows = (await db.execute(select(UploadSession).where(
        UploadSession.user_id == auth.user.id, UploadSession.state == "pending",
        UploadSession.expires_at > utcnow(),
    ).order_by(UploadSession.created_at))).scalars()
    return {"ok": True, "items": [payload(row) for row in rows]}


@router.post("", status_code=201, dependencies=[Depends(check_csrf)])
async def create_upload(body: UploadIn, db=Depends(get_db), auth=Depends(require_user)):
    uid = auth.user.id
    await db.rollback()
    # Serialize all quota reservations across workers without holding the DB during network I/O.
    await db.execute(text("BEGIN IMMEDIATE"))
    user = await db.get(User, uid, populate_existing=True)
    if user is None or not user.is_active:
        raise HTTPException(401, "请重新登录")
    name = storage.safe_filename(body.name)
    existing = (await db.execute(select(UploadSession).where(
        UploadSession.user_id == uid, UploadSession.state == "pending",
        UploadSession.expires_at > utcnow(), UploadSession.sha256 == body.sha256,
        UploadSession.size == body.size, UploadSession.name == name,
    ).limit(1))).scalar_one_or_none()
    if existing:
        result = payload(existing)
        await db.rollback()
        return {"ok": True, "upload": result, "resumed": True}
    if not upload_limiter.check(str(uid)):
        raise HTTPException(429, "上传过于频繁，请稍后再试")
    cfg = await settings_service.template_settings(db)
    if body.size > int(cfg["max_upload_mb"]) * 1024**2:
        raise HTTPException(413, "文件超过单文件大小上限")
    await expiry(db, body.expires_hours, user)
    count, reserved_bytes = await uploads.reserved(db, uid)
    q = await quota.resolve_quota(db, user)
    if count >= max(1, uploads.settings.upload_max_pending):
        raise HTTPException(403, "未完成上传任务过多，请先完成或放弃部分任务")
    if not q.unlimited_files and q.used_files + count + 1 > q.max_files:
        raise HTTPException(403, "文件数量配额不足（含未完成上传）")
    if not q.unlimited_bytes and q.used_bytes + reserved_bytes + body.size > q.max_bytes:
        raise HTTPException(403, "存储空间配额不足（含未完成上传）")
    total, remaining = (await db.execute(select(
        func.coalesce(func.sum(UploadSession.size), 0),
        func.coalesce(func.sum(UploadSession.size - UploadSession.offset), 0),
    ).where(UploadSession.state == "pending"))).one()
    if int(total) + body.size > max(1, uploads.settings.upload_temp_limit_gb) * 1024**3:
        raise HTTPException(507, "临时上传空间已满，请稍后再试")
    free = shutil.disk_usage(uploads.settings.data_dir).free
    if free < int(remaining) + body.size + max(0, uploads.settings.upload_disk_reserve_mb) * 1024**2:
        raise HTTPException(507, "服务器可用磁盘空间不足")
    row = UploadSession(id=uuid.uuid4().hex, user_id=uid, name=name, size=body.size,
                        sha256=body.sha256, offset=0, expires_hours=body.expires_hours,
                        is_public=body.is_public, stored_path=storage.make_rel_path(),
                        state="pending", expires_at=uploads.deadline())
    db.add(row)
    await db.commit()
    return {"ok": True, "upload": payload(row), "resumed": False}


@router.get("/{upload_id}")
async def upload_status(upload_id: str, db=Depends(get_db), auth=Depends(require_user)):
    row = await owned(db, upload_id, auth.user.id, pending=False)
    if row.state == "pending" and row.expires_at <= utcnow():
        raise HTTPException(410, "上传任务已过期，请重新上传")
    result = {"ok": True, "upload": payload(row)}
    if row.state == "completed":
        item = await db.get(FileItem, row.file_id) if row.file_id else None
        if item is None:
            raise HTTPException(410, "文件已被删除")
        result["file"] = _serialize(item)
    return result


@router.patch("/{upload_id}", dependencies=[Depends(check_csrf)])
async def append_chunk(upload_id: str, request: Request,
                       upload_offset: int = Header(ge=0),
                       x_chunk_sha256: str = Header(pattern=r"^[0-9a-f]{64}$"),
                       db=Depends(get_db), auth=Depends(require_user)):
    uid = auth.user.id
    await owned(db, upload_id, uid)
    await db.rollback()
    with uploads.upload_lock(upload_id):
        row = await owned(db, upload_id, uid)
        if upload_offset != row.offset:
            raise HTTPException(409, "分片位置不一致，请重新获取上传进度")
        if row.offset >= row.size:
            raise HTTPException(409, "文件已经传完，请完成上传")
        limit = min(uploads.CHUNK_BYTES, row.size - row.offset)
        declared = request.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > limit):
            raise HTTPException(413, "分片超过允许的大小")
        offset = row.offset
        await db.rollback()
        data = bytearray()
        stream = request.stream().__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), timeout=120)
                except StopAsyncIteration:
                    break
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "分片超过允许的大小")
                data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "分片接收超时，已完成的分片可以续传") from None
        if not data:
            raise HTTPException(400, "分片内容为空")
        if hashlib.sha256(data).hexdigest() != x_chunk_sha256:
            raise HTTPException(422, "分片校验失败，请重传当前分片")
        path = uploads.part_path(upload_id)

        def write():
            with open(path, "r+b" if path.exists() else "w+b") as handle:
                if handle.seek(0, 2) < offset:
                    raise HTTPException(409, "临时文件不完整，请放弃任务后重新上传")
                handle.truncate(offset)  # Discard only bytes never acknowledged in the DB.
                handle.seek(offset)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

        # Keep the kernel lock until a cancelled request's disk write has actually stopped.
        task = asyncio.create_task(run_in_threadpool(write))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except OSError:
            raise HTTPException(507, "磁盘写入失败，已完成的分片仍可续传") from None
        row = await owned(db, upload_id, uid)
        row.offset = offset + len(data)
        row.updated_at = utcnow()
        row.expires_at = uploads.deadline()
        await db.commit()
        return {"ok": True, "upload": payload(row)}


@router.post("/{upload_id}/complete", dependencies=[Depends(check_csrf)])
async def complete_upload(upload_id: str, request: Request,
                          db=Depends(get_db), auth=Depends(require_user)):
    uid = auth.user.id
    await owned(db, upload_id, uid, pending=False)
    await db.rollback()
    with uploads.upload_lock(upload_id):
        row = await owned(db, upload_id, uid, pending=False)
        if row.state == "completed":
            item = await db.get(FileItem, row.file_id) if row.file_id else None
            if item is None:
                raise HTTPException(410, "文件已被删除")
            return {"ok": True, "file": _serialize(item)}
        if row.state != "pending" or row.expires_at <= utcnow():
            raise HTTPException(410, "上传任务已过期或结束")
        if row.offset != row.size:
            raise HTTPException(409, "文件尚未传完")
        dest = storage.resolve(row.stored_path)
        source = uploads.part_path(upload_id)
        # A previous worker may have renamed successfully and crashed before DB commit.
        source = source if source.exists() else dest
        expected, size = row.sha256, row.size
        await db.rollback()
        if not source.exists() or source.stat().st_size != size:
            raise HTTPException(409, "临时文件不完整，请重新上传")
        task = asyncio.create_task(run_in_threadpool(uploads.hash_file, source))
        try:
            digest = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        if digest != expected:
            source.unlink(missing_ok=True)
            row = await owned(db, upload_id, uid)
            row.state, row.updated_at = "failed", utcnow()
            await db.commit()
            raise HTTPException(422, "文件完整性校验失败，请重新上传")
        await db.execute(text("BEGIN IMMEDIATE"))
        row = await owned(db, upload_id, uid)
        user = await db.get(User, uid, populate_existing=True)
        if user is None or not user.is_active:
            raise HTTPException(401, "请重新登录")
        expires_at = await expiry(db, row.expires_hours, user)
        cfg = await settings_service.template_settings(db)
        if size > int(cfg["max_upload_mb"]) * 1024**2:
            raise HTTPException(413, "文件超过当前单文件大小上限")
        q = await quota.resolve_quota(db, user)
        if ((not q.unlimited_files and q.used_files + 1 > q.max_files)
                or (not q.unlimited_bytes and q.used_bytes + size > q.max_bytes)):
            raise HTTPException(403, "当前配额不足，请释放空间或放弃上传")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source != dest:
            os.replace(source, dest)
        item = FileItem(public_id=uuid.uuid4().hex[:16], user_id=uid, original_name=row.name,
                        stored_path=row.stored_path, size=size, mime_type=storage.guess_mime(row.name),
                        sha256=digest, is_public=row.is_public, uploaded_ip=client_ip(request),
                        expires_at=expires_at)
        db.add(item)
        await quota.consume(db, uid, size)
        await db.flush()
        row.file_id, row.state, row.updated_at = item.id, "completed", utcnow()
        await db.commit()
        result = _serialize(item)
    await log_activity(action=Action.UPLOAD, user=user, request=request,
                       target_type="file", target_id=item.id,
                       detail={"filename": item.original_name, "size": size,
                               "sha256": digest, "resumable": True})
    return {"ok": True, "file": result}


@router.delete("/{upload_id}", dependencies=[Depends(check_csrf)])
async def cancel_upload(upload_id: str, db=Depends(get_db), auth=Depends(require_user)):
    uid = auth.user.id
    await owned(db, upload_id, uid, pending=False)
    await db.rollback()
    with uploads.upload_lock(upload_id):
        row = await owned(db, upload_id, uid, pending=False)
        if row.state == "completed":
            raise HTTPException(409, "文件已完成上传，请从文件列表删除")
        uploads.part_path(upload_id).unlink(missing_ok=True)
        await storage.delete_object(row.stored_path)
        row.state, row.updated_at = "cancelled", utcnow()
        await db.commit()
    return {"ok": True}
