"""匿名上传接口：未登录用户上传文件（后台「匿名上传」开启后可用）。

限额与日志见 :mod:`app.services.anonymous`。流程与登录用户的上传相同：先做不读正文的预检，
再占接收名额、流式落盘，最后在写锁内复核限额并入库。任何一步被拒绝都写一条拒绝日志。
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.deps import check_csrf
from ..core.security import client_ip
from ..services import anonymous, storage
from ..services.anonymous import Rejected
from .files import _serialize

router = APIRouter(prefix="/api/anon", tags=["anonymous"])


def _expires_hours(raw: object, lim: anonymous.Limits) -> int:
    """保留时长：1 小时到后台设定的上限（最多 24 小时），不填取上限。"""
    if raw in (None, ""):
        return lim.max_expire_hours
    try:
        hours = int(str(raw))
    except ValueError:
        raise Rejected(400, "bad_request", "保留时长不合法") from None
    return max(1, min(hours, lim.max_expire_hours))


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise Rejected(400, "bad_request", "请求体长度不合法") from None
    if value < 0:
        raise Rejected(400, "bad_request", "请求体长度不合法")
    return value


@router.post("/files", status_code=status.HTTP_201_CREATED)
async def anonymous_upload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    key = anonymous.limit_key(client_ip(request))
    try:
        return await _upload(request, db, key)
    except Rejected as exc:
        await anonymous.log_rejection(request, key, exc.reason, exc.message)
        headers = {"Retry-After": "60"} if exc.status_code == 429 else None
        raise HTTPException(exc.status_code, exc.message, headers=headers) from None


async def _upload(request: Request, db: AsyncSession, key: str) -> JSONResponse:
    from ..services.incoming import IncomingUpload
    from ..services.upload_slots import UploadBusy, receive_slot

    lim = await anonymous.limits(db)
    if not lim.enabled:
        raise Rejected(403, "disabled", "本站未开放匿名上传，请登录后上传")
    if not anonymous.request_limiter.check(key):
        raise Rejected(429, "rate_limited", "上传请求过于频繁，请稍后再试")
    expires_hours = _expires_hours(request.query_params.get("expires_hours"), lim)
    declared = _declared_length(request)
    try:
        upload = IncomingUpload(request, max_bytes=lim.max_bytes)
    except storage.StorageError as exc:
        raise Rejected(400, "bad_request", str(exc)) from exc
    overhead = 65536 if upload.multipart else 0
    if declared is not None and declared > lim.max_bytes + overhead:
        raise Rejected(413, "too_large", f"匿名上传单个文件不能超过 {anonymous.storage_size(lim.max_bytes)}")
    # 预检：正文还没读，先按声明的大小把超额的请求挡掉
    limit = await anonymous.check(db, key, 0 if upload.multipart else (declared or 0), lim)
    if limit <= 0:
        raise Rejected(429, "ip_daily_bytes", "本网络 24 小时内的匿名上传额度已用完，请登录后上传或稍后再试")
    if declared is not None:
        limit = min(limit, declared)
    # 接收正文时不占数据库连接
    await db.close()
    ip_lock = "anon-ip-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    groups = [(ip_lock, 1, "同一网络同时只能匿名上传一个文件，请等当前文件完成"),
              ("anon-all", lim.max_concurrent, "匿名上传繁忙，请稍后再试")]
    try:
        with receive_slot(None, limit, groups=groups):
            try:
                stored = await storage.store_upload(upload, max_bytes=limit)
            except storage.UploadTooLarge as exc:
                raise Rejected(413, "too_large", f"文件超过允许的体积（本次上限 {anonymous.storage_size(exc.limit)}）") from exc
            except storage.StorageError as exc:
                raise Rejected(400, "upload_failed", str(exc)) from exc
    except UploadBusy as exc:
        raise Rejected(429, "busy", str(exc)) from exc

    try:
        if upload.multipart:
            expires_hours = _expires_hours(upload.fields.get("expires_hours"), lim)
        item = await anonymous.save(db, request, key=key, stored=stored, filename=upload.filename,
                                    expires_hours=expires_hours, lim=lim)
    except BaseException:
        await db.rollback()
        await storage.delete_object(stored.rel_path)
        raise
    return JSONResponse({"ok": True, "file": _serialize(item)}, status_code=201)
