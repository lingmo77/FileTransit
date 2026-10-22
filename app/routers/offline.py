"""Offline download APIs. Every mutation persists intent before touching the engine."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.deps import AuthContext, check_csrf, require_user
from ..models import User
from ..services import offline, quota, settings_service, storage
from ..services.activity import log_activity
from ..services.incoming import IncomingUpload

router = APIRouter(prefix="/api/offline/tasks", tags=["offline"])


class CreateLinksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=20)
    expires_hours: int | None = Field(default=None, ge=0, le=87600)
    is_public: bool = False
    request_key: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indexes: list[int] = Field(min_length=1, max_length=100000)


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(pause|resume|cancel|retry|remove)$")


def _http_error(exc):
    if isinstance(exc, quota.QuotaExceeded):
        return HTTPException(403, exc.message)
    return HTTPException(exc.status_code, str(exc))


@router.get("")
async def get_tasks(
    page: int = Query(1, ge=1), state: str = Query("all", max_length=32),
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(require_user),
):
    try:
        result = await offline.list_tasks(db, auth.user, page=page, state=state)
        # The worker is optional during migrations and tests; service status never exposes RPC secrets.
        from ..services.offline_worker import service_status
        result["service"] = await service_status(db)
        return result
    except offline.OfflineError as exc:
        raise _http_error(exc) from exc


@router.post("", status_code=201)
async def create_tasks(
    payload: CreateLinksIn, request: Request,
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
):
    user_id = auth.user.id
    tasks, errors = [], []
    for index, value in enumerate(payload.urls):
        try:
            user = await db.get(User, user_id, populate_existing=True)
            if user is None or not user.is_active:
                raise offline.OfflineError("用户不存在或已被停用", 403)
            task = await offline.create_link_task(
                db, user, value, expires_hours=payload.expires_hours, is_public=payload.is_public,
                request_key=f"{payload.request_key}:{index}" if payload.request_key else None)
            tasks.append(await offline.serialize_task(db, task))
            await log_activity(action="offline_create", user=user, request=request,
                               target_type="offline_task", target_id=task.id,
                               detail={"kind": task.kind, "is_public": task.is_public, "expires_hours": task.expires_hours})
        except (offline.OfflineError, quota.QuotaExceeded) as exc:
            await db.rollback()
            if len(payload.urls) == 1:
                raise _http_error(exc) from exc
            errors.append({"index": index, "error": str(exc)})
    return {"ok": True, "tasks": tasks, "errors": errors, "task": tasks[0] if len(tasks) == 1 else None}


@router.post("/torrent", status_code=201)
async def create_torrent(
    request: Request, db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user), _csrf: None = Depends(check_csrf),
):
    try:
        await offline.ensure_enabled(db)
        limit = max(1, await settings_service.get_int(db, "offline.metadata_max_mb", 5)) * 1024**2
        incoming = IncomingUpload(request, max_bytes=limit)
        content = bytearray()
        while chunk := await incoming.read(64 * 1024):
            content.extend(chunk)
            if len(content) > limit:
                raise offline.OfflineError("种子超过允许的大小", 413)
        options = incoming.fields if incoming.multipart else request.query_params
        try:
            hours = int(options["expires_hours"]) if "expires_hours" in options else None
            public = str(options.get("is_public", "false")).lower()
            if public not in {"true", "false"}:
                raise ValueError
            request_key = options.get("request_key")
            if request_key is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(request_key)):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise offline.OfflineError("种子任务参数不合法") from exc
        task = await offline.create_torrent_task(db, auth.user, bytes(content),
                    expires_hours=hours, is_public=public == "true", request_key=request_key)
        await log_activity(action="offline_create", user=auth.user, request=request,
                           target_type="offline_task", target_id=task.id,
                           detail={"kind": "torrent", "is_public": task.is_public, "expires_hours": task.expires_hours})
        return {"ok": True, "task": await offline.serialize_task(db, task)}
    except (offline.OfflineError, quota.QuotaExceeded) as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except storage.UploadTooLarge as exc:
        raise HTTPException(413, "种子超过允许的大小") from exc
    except storage.StorageError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(require_user)):
    try:
        task = await offline.get_owned_task(db, auth.user, task_id)
        return {"ok": True, "task": await offline.serialize_task(db, task)}
    except offline.OfflineError as exc:
        raise _http_error(exc) from exc


@router.post("/{task_id}/select")
async def choose_files(task_id: str, payload: SelectionIn, request: Request,
                       db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(require_user),
                       _csrf: None = Depends(check_csrf)):
    try:
        task = await offline.select_files(db, auth.user, task_id, payload.indexes)
        await log_activity(action="offline_select", user=auth.user, request=request,
                           target_type="offline_task", target_id=task.id,
                           detail={"selected_count": task.reserved_files})
        return {"ok": True, "task": await offline.serialize_task(db, task)}
    except (offline.OfflineError, quota.QuotaExceeded) as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.post("/{task_id}/action")
async def act_on_task(task_id: str, payload: ActionIn, request: Request,
                      db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(require_user),
                      _csrf: None = Depends(check_csrf)):
    try:
        task = await offline.action_task(db, auth.user, task_id, payload.action)
        await log_activity(action="offline_" + payload.action, user=auth.user, request=request,
                           target_type="offline_task", target_id=task.id)
        return {"ok": True, "task": await offline.serialize_task(db, task)}
    except (offline.OfflineError, quota.QuotaExceeded) as exc:
        await db.rollback()
        raise _http_error(exc) from exc
