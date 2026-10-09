"""文件接口：上传、删除、修改、配额查询。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import get_db
from ..core.deps import AuthContext, check_csrf, require_user
from ..core.security import client_ip, new_token, upload_limiter
from ..core.templating import fmt_iso
from ..models import FileItem
from ..services import files as files_service
from ..services import quota as quota_service
from ..services import settings_service, storage
from ..services.activity import Action, log_activity
from ..services.pagination import Page

router = APIRouter(prefix="/api/files", tags=["files"])
me_router = APIRouter(prefix="/api/me", tags=["me"])


class FileUpdateIn(BaseModel):
    #: ``0`` = 长期有效，仅管理员可用（见 :func:`files_service.resolve_expiry`）
    expires_hours: int | None = Field(default=None, ge=0, le=24 * 365)
    is_public: bool | None = None


def _serialize(item: FileItem) -> dict[str, object]:
    return {
        "id": item.id,
        "public_id": item.public_id,
        "name": item.original_name,
        "size": item.size,
        "mime_type": item.mime_type,
        "download_count": item.download_count,
        "is_public": item.is_public,
        # fmt_iso 补上 +00:00，否则前端 new Date() 会按浏览器本地时区解析
        "created_at": fmt_iso(item.created_at),
        "expires_at": fmt_iso(item.expires_at) or None,
        "forever": item.expires_at is None,
        "download_url": f"/download/{item.public_id}/{item.original_name}",
        "detail_url": f"/f/{item.public_id}",
    }


async def _resolve_expiry(
    db: AsyncSession, expires_hours: int, auth: AuthContext
) -> datetime | None:
    try:
        return await files_service.resolve_expiry(
            db, expires_hours, is_admin=auth.user.is_admin
        )
    except files_service.ForeverNotAllowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "普通用户不能设置长期有效") from None


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    expires_hours: int = Form(72),
    is_public: bool = Form(True),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    if not upload_limiter.check(str(auth.user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "上传过于频繁，请稍后再试")

    cfg = await settings_service.template_settings(db)
    site_limit = int(cfg["max_upload_mb"]) * 1024 * 1024

    # 明知会超限时提前拒绝，省掉整段上传
    declared = getattr(file, "size", None)
    if declared is not None and declared > site_limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超过单文件上限 {cfg['max_upload_mb']} MB",
        )

    quota = await quota_service.resolve_quota(db, auth.user)
    remaining = quota.remaining_bytes
    limit = site_limit if remaining is None else min(site_limit, remaining)

    if remaining is not None and remaining <= 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "存储空间已用尽，请先删除部分文件")

    # 必须在落盘之前：普通用户传 expires_hours=0 会在这里被拒，
    # 放到 store_upload 之后就会在磁盘上留下没有数据库记录的孤儿文件
    expires_at = await _resolve_expiry(db, expires_hours, auth)

    try:
        stored = await storage.store_upload(file, max_bytes=limit)
    except storage.UploadTooLarge as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超过允许的体积（本次上限 {exc.limit // 1024 // 1024} MB）",
        ) from exc
    except storage.StorageError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    original_name = storage.safe_filename(file.filename or "unnamed")
    try:
        # 落盘之后再复核一次配额，避免并发上传把用量撑爆
        await quota_service.ensure_can_upload(db, auth.user, stored.size)
    except quota_service.QuotaExceeded as exc:
        await storage.delete_object(stored.rel_path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.message) from exc

    item = FileItem(
        public_id=new_token(8),
        user_id=auth.user.id,
        original_name=original_name,
        stored_path=stored.rel_path,
        size=stored.size,
        mime_type=storage.guess_mime(original_name),
        sha256=stored.sha256,
        is_public=is_public,
        uploaded_ip=client_ip(request),
        expires_at=expires_at,
    )
    db.add(item)
    await quota_service.consume(db, auth.user.id, stored.size)
    await db.commit()
    await db.refresh(item)

    await log_activity(
        action=Action.UPLOAD,
        user=auth.user,
        request=request,
        target_type="file",
        target_id=item.id,
        detail={
            "filename": original_name,
            "size": stored.size,
            "sha256": stored.sha256,
            "expires_hours": (
                files_service.FOREVER_LABEL if expires_at is None else expires_hours
            ),
            "is_public": is_public,
        },
    )

    return JSONResponse({"ok": True, "file": _serialize(item)}, status_code=201)


@router.delete("/{file_id}")
async def delete_file(
    file_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    item = await files_service.get_by_id(db, file_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    if item.user_id != auth.user.id and not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能删除自己的文件")

    await storage.delete_object(item.stored_path)
    await db.delete(item)
    await quota_service.release(db, item.user_id, item.size)
    await db.commit()

    await log_activity(
        action=Action.DELETE_FILE,
        user=auth.user,
        request=request,
        target_type="file",
        target_id=item.id,
        detail={"filename": item.original_name, "size": item.size},
    )
    return JSONResponse({"ok": True, "message": "文件已删除"})


@router.patch("/{file_id}")
async def update_file(
    file_id: int,
    payload: FileUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    item = await files_service.get_by_id(db, file_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    if item.user_id != auth.user.id and not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能修改自己的文件")

    changes: dict[str, object] = {}
    if payload.expires_hours is not None:
        item.expires_at = await _resolve_expiry(db, payload.expires_hours, auth)
        changes["expires_hours"] = (
            files_service.FOREVER_LABEL if item.expires_at is None else payload.expires_hours
        )
    if payload.is_public is not None:
        item.is_public = payload.is_public
        changes["is_public"] = payload.is_public

    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的内容")

    await db.commit()
    await db.refresh(item)

    await log_activity(
        action=Action.UPDATE_FILE,
        user=auth.user,
        request=request,
        target_type="file",
        target_id=item.id,
        detail={"filename": item.original_name, **changes},
    )
    return JSONResponse({"ok": True, "file": _serialize(item)})


@router.get("/{file_id}")
async def file_info(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> JSONResponse:
    item = await files_service.get_by_id(db, file_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    if item.user_id != auth.user.id and not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看")
    return JSONResponse({"ok": True, "file": _serialize(item)})


# ------------------------------------------------------------------ 配额


def _page_payload(page: Page) -> dict[str, object]:
    return {
        "page": page.page,
        "per_page": page.per_page,
        "total": page.total,
        "total_pages": page.total_pages,
        "items": [_serialize(item) for item in page.items],
    }


@me_router.get("/quota")
async def my_quota(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> JSONResponse:
    quota = await quota_service.resolve_quota(db, auth.user)
    cfg = await settings_service.template_settings(db)
    return JSONResponse(
        {
            "ok": True,
            "used_files": quota.used_files,
            "max_files": quota.max_files,
            "used_bytes": quota.used_bytes,
            "max_bytes": quota.max_bytes,
            "remaining_files": quota.remaining_files,
            "remaining_bytes": quota.remaining_bytes,
            "max_expire_days": cfg["max_expire_days"],
            "max_upload_mb": cfg["max_upload_mb"],
        }
    )


__all__ = ["me_router", "router", "_page_payload", "_serialize", "settings"]
