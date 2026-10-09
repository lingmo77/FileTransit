"""管理员后台接口。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.deps import AuthContext, check_csrf, require_admin
from ..core.security import hash_password, password_problem
from ..models import User, utcnow
from ..services import cleanup, logs as logs_service, mailer, stats, storage
from ..services import files as files_service
from ..services import quota as quota_service
from ..services import settings_service
from ..services import users as users_service
from ..services.activity import Action, log_activity
from .files import _page_payload, _serialize

router = APIRouter(
    prefix="/admin/api",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ------------------------------------------------------------------ 仪表盘


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    data = await stats.overview(db)
    data.pop("recent_files", None)
    return JSONResponse({"ok": True, "stats": data})


@router.post("/maintenance/run")
async def run_maintenance(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    result = await cleanup.run_maintenance()
    await log_activity(
        action=Action.EXPIRE_CLEANUP,
        user=auth.user,
        request=request,
        detail={"manual": True, **result},
    )
    return JSONResponse({"ok": True, "result": result})


# ------------------------------------------------------------------ 用户管理


class UserCreateIn(BaseModel):
    username: str = Field(max_length=64)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)
    is_admin: bool = False
    email_verified: bool = True


class UserUpdateIn(BaseModel):
    email: str | None = None
    password: str | None = None
    is_active: bool | None = None
    is_admin: bool | None = None
    email_verified: bool | None = None
    quota_max_files: int | None = Field(default=None, ge=-1, le=1_000_000)
    quota_max_bytes: int | None = Field(default=None, ge=-1)


def _serialize_user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "email_verified": user.email_verified,
        "must_change_password": user.must_change_password,
        "quota_max_files": user.quota_max_files,
        "quota_max_bytes": user.quota_max_bytes,
        "used_files": user.used_files,
        "used_bytes": user.used_bytes,
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "last_login_ip": user.last_login_ip,
    }


@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    status_filter: str = Query("", alias="status", max_length=16),
) -> JSONResponse:
    result = await users_service.list_users(
        db, page=page, q=q.strip() or None, status=status_filter.strip() or None
    )
    return JSONResponse(
        {
            "ok": True,
            "page": result.page,
            "total": result.total,
            "total_pages": result.total_pages,
            "items": [_serialize_user(u) for u in result.items],
        }
    )


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    from ..services import account
    from ..services import auth as auth_service

    try:
        username = account.validate_username(payload.username)
        email = account.validate_email(payload.email)
    except account.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    problem = password_problem(payload.password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    if await auth_service.username_taken(db, username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该用户名已被占用")
    if await auth_service.find_user_by_email(db, email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该邮箱已被注册")

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
        email_verified=payload.email_verified,
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await log_activity(
        action=Action.ADMIN_USER_UPDATE,
        user=auth.user,
        request=request,
        target_type="user",
        target_id=user.id,
        detail={"op": "create", "username": user.username, "email": user.email},
    )
    return JSONResponse({"ok": True, "user": _serialize_user(user)}, status_code=201)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    user = await users_service.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    data = payload.model_dump(exclude_unset=True)
    changes: dict[str, object] = {}

    if "email" in data and data["email"]:
        from ..services import account
        from ..services import auth as auth_service

        try:
            email = account.validate_email(str(data["email"]))
        except account.AccountError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
        existing = await auth_service.find_user_by_email(db, email)
        if existing is not None and existing.id != user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "该邮箱已被其它账号使用")
        user.email = email
        user.email_verified = False
        changes["email"] = email

    if data.get("password"):
        problem = password_problem(str(data["password"]))
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
        user.password_hash = hash_password(str(data["password"]))
        user.must_change_password = True
        changes["password"] = "***"

    if data.get("is_admin") is not None:
        if user.id == auth.user.id and not data["is_admin"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能取消自己的管理员权限")
        if not data["is_admin"] and await users_service.count_admins(db, exclude_id=user.id) == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一名管理员")
        user.is_admin = bool(data["is_admin"])
        changes["is_admin"] = user.is_admin

    if data.get("is_active") is not None:
        if user.id == auth.user.id and not data["is_active"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用当前登录的账号")
        user.is_active = bool(data["is_active"])
        changes["is_active"] = user.is_active

    if data.get("email_verified") is not None:
        user.email_verified = bool(data["email_verified"])
        changes["email_verified"] = user.email_verified

    if "quota_max_files" in data:
        user.quota_max_files = data["quota_max_files"]
        changes["quota_max_files"] = user.quota_max_files
    if "quota_max_bytes" in data:
        user.quota_max_bytes = data["quota_max_bytes"]
        changes["quota_max_bytes"] = user.quota_max_bytes

    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的内容")

    await db.commit()
    await db.refresh(user)

    await log_activity(
        action=Action.ADMIN_USER_UPDATE,
        user=auth.user,
        request=request,
        target_type="user",
        target_id=user.id,
        detail={"op": "update", "username": user.username, **changes},
    )
    return JSONResponse({"ok": True, "user": _serialize_user(user)})


@router.post("/users/{user_id}/quota/recount")
async def recount_quota(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    user = await users_service.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    count, total = await quota_service.sync_usage(db, user.id)
    await db.commit()
    await log_activity(
        action=Action.ADMIN_USER_UPDATE,
        user=auth.user,
        request=request,
        target_type="user",
        target_id=user.id,
        detail={"op": "recount_quota", "files": count, "bytes": total},
    )
    return JSONResponse({"ok": True, "used_files": count, "used_bytes": total})


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    user = await users_service.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if user.id == auth.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除当前登录的账号")
    if user.is_admin and await users_service.count_admins(db, exclude_id=user.id) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一名管理员")

    username = user.username
    file_count, freed = await users_service.delete_user(db, user)

    await log_activity(
        action=Action.ADMIN_USER_UPDATE,
        user=auth.user,
        request=request,
        target_type="user",
        target_id=user_id,
        detail={"op": "delete", "username": username, "files": file_count, "freed_bytes": freed},
    )
    return JSONResponse(
        {"ok": True, "message": f"已删除用户 {username}，同时删除 {file_count} 个文件"}
    )


# ------------------------------------------------------------------ 文件管理


@router.get("/files")
async def list_files(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    user_id: int | None = Query(None),
    expired: bool = Query(False),
) -> JSONResponse:
    result = await files_service.list_admin(
        db, page=page, q=q.strip() or None, user_id=user_id, expired=expired
    )
    return JSONResponse({"ok": True, **_page_payload(result)})


class AdminFileUpdateIn(BaseModel):
    #: ``0`` = 长期有效；管理员改期不受 ``quota.max_expire_days`` 限制
    expires_hours: int | None = Field(default=None, ge=0, le=24 * 3650)
    is_public: bool | None = None
    original_name: str | None = Field(default=None, max_length=255)


@router.patch("/files/{file_id}")
async def update_file(
    file_id: int,
    payload: AdminFileUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    item = await files_service.get_by_id(db, file_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")

    changes: dict[str, object] = {}
    if payload.expires_hours is not None:
        if payload.expires_hours <= files_service.FOREVER_HOURS:
            item.expires_at = None
            changes["expires_hours"] = files_service.FOREVER_LABEL
        else:
            item.expires_at = utcnow() + timedelta(hours=payload.expires_hours)
            changes["expires_hours"] = payload.expires_hours
    if payload.is_public is not None:
        item.is_public = payload.is_public
        changes["is_public"] = payload.is_public
    if payload.original_name:
        item.original_name = storage.safe_filename(payload.original_name)
        changes["original_name"] = item.original_name

    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的内容")

    await db.commit()
    await db.refresh(item)
    await log_activity(
        action=Action.ADMIN_DELETE_FILE,
        user=auth.user,
        request=request,
        target_type="file",
        target_id=item.id,
        detail={"op": "update", **changes},
    )
    return JSONResponse({"ok": True, "file": _serialize(item)})


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    item = await files_service.get_by_id(db, file_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")

    detail = {"filename": item.original_name, "size": item.size, "owner": item.user_id}
    await storage.delete_object(item.stored_path)
    await db.delete(item)
    await quota_service.release(db, item.user_id, item.size)
    await db.commit()

    await log_activity(
        action=Action.ADMIN_DELETE_FILE,
        user=auth.user,
        request=request,
        target_type="file",
        target_id=file_id,
        detail={"op": "delete", **detail},
    )
    return JSONResponse({"ok": True, "message": "文件已删除"})


# ------------------------------------------------------------------ 日志


@router.get("/logs")
async def list_logs(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    action: str = Query("", max_length=48),
    status_filter: str = Query("", alias="status", max_length=16),
    user_id: int | None = Query(None),
    ip: str = Query("", max_length=64),
    start: str = Query("", max_length=32),
    end: str = Query("", max_length=32),
) -> JSONResponse:
    tz = await logs_service.site_tz(db)
    result = await logs_service.list_logs(
        db,
        page=page,
        q=q.strip() or None,
        action=action.strip() or None,
        status_filter=status_filter.strip() or None,
        user_id=user_id,
        ip=ip.strip() or None,
        start=logs_service.parse_date(start, tz_name=tz),
        end=logs_service.parse_date(end, tz_name=tz, end_of_day=True),
    )
    return JSONResponse(
        {
            "ok": True,
            "page": result.page,
            "per_page": result.per_page,
            "total": result.total,
            "total_pages": result.total_pages,
            "counts": await logs_service.action_counts(db),
            "items": [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "username": row.username,
                    "user_id": row.user_id,
                    "action": row.action,
                    "action_label": logs_service.ACTION_LABELS.get(row.action, row.action),
                    "status": row.status,
                    "ip_address": row.ip_address,
                    "browser": row.browser,
                    "os_name": row.os_name,
                    "device": row.device,
                    "method": row.method,
                    "path": row.path,
                    "target": f"{row.target_type}:{row.target_id}"
                    if row.target_type
                    else None,
                    "detail": row.detail,
                }
                for row in result.items
            ],
        }
    )


class LogPurgeIn(BaseModel):
    before: str = ""
    after: str = ""
    action: str = ""
    status_filter: str = Field(default="", alias="status")
    vacuum: bool = True

    model_config = {"populate_by_name": True}


@router.delete("/logs")
async def purge_logs(
    payload: LogPurgeIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    tz = await logs_service.site_tz(db)
    # before 取当天 23:59:59（站点时区），让"清空 X 之前的日志"包含 X 全天
    before = logs_service.parse_date(payload.before, tz_name=tz, end_of_day=True)
    after = logs_service.parse_date(payload.after, tz_name=tz)
    if payload.before and before is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "起始时间格式不正确")
    if payload.after and after is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "结束时间格式不正确")

    removed = await logs_service.purge_logs(
        db,
        before=before,
        after=after,
        action=payload.action.strip() or None,
        status_filter=payload.status_filter.strip() or None,
    )

    if payload.vacuum:
        await cleanup.vacuum_database()

    await log_activity(
        action=Action.ADMIN_LOGS_PURGE,
        user=auth.user,
        request=request,
        detail={
            "removed": removed,
            "before": payload.before,
            "after": payload.after,
            "action": payload.action,
            "vacuum": payload.vacuum,
        },
    )
    return JSONResponse(
        {"ok": True, "removed": removed, "message": f"已清理 {removed} 条日志"}
    )


@router.get("/logs/export", response_class=PlainTextResponse)
async def export_logs(
    db: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=120),
    action: str = Query("", max_length=48),
    status_filter: str = Query("", alias="status", max_length=16),
    user_id: int | None = Query(None),
    ip: str = Query("", max_length=64),
    start: str = Query("", max_length=32),
    end: str = Query("", max_length=32),
) -> PlainTextResponse:
    tz = await logs_service.site_tz(db)
    rows = await logs_service.fetch_for_export(
        db,
        q=q.strip() or None,
        action=action.strip() or None,
        status_filter=status_filter.strip() or None,
        user_id=user_id,
        ip=ip.strip() or None,
        start=logs_service.parse_date(start, tz_name=tz),
        end=logs_service.parse_date(end, tz_name=tz, end_of_day=True),
    )
    csv_text = logs_service.export_csv(rows, tz_name=tz)
    filename = f"logs-{utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return PlainTextResponse(
        # 加 BOM，Excel 打开中文不乱码
        "﻿" + csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ 站点设置


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    values = await settings_service.all_values(db, force=True)
    return JSONResponse({"ok": True, "settings": _mask_settings(values)})


def _mask_settings(values: dict) -> dict:
    masked = {k: v for k, v in values.items() if k not in settings_service.SECRET_KEYS}
    masked["email.has_password"] = bool(values.get("email.password_enc"))
    return masked


@router.post("/settings")
async def save_settings(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    updates: dict[str, object] = {}
    for key, value in payload.items():
        # 密文字段只能由下面的专用逻辑写入，不能被请求体直接赋值
        if key in settings_service.SECRET_KEYS or key.startswith("email.password"):
            continue
        # email.has_password 是只读的派生字段，不是真正的设置项
        if key not in settings_service.DEFAULTS:
            continue
        updates[key] = value

    # 密码单独处理：留空表示不改动，其它情况加密存储
    password = payload.get("email.password")
    if isinstance(password, str) and password.strip():
        updates["email.password_enc"] = settings_service.encrypt_secret(password.strip())

    if not updates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要保存的设置")

    await settings_service.set_many(db, updates, updated_by=auth.user.id)

    safe_log = {k: v for k, v in updates.items() if k not in settings_service.SECRET_KEYS}
    await log_activity(
        action=Action.ADMIN_SETTINGS_UPDATE,
        user=auth.user,
        request=request,
        detail={"keys": list(safe_log.keys()), "values": safe_log},
    )

    values = await settings_service.all_values(db, force=True)
    return JSONResponse({"ok": True, "settings": _mask_settings(values), "message": "设置已保存"})


# ------------------------------------------------------------------ 邮件


class TestMailIn(BaseModel):
    to: str = Field(max_length=254)


@router.post("/email/test")
async def test_email(
    payload: TestMailIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    from ..services import account

    try:
        recipient = account.validate_email(payload.to)
    except account.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    try:
        await mailer.send_test_email(db, to=recipient)
    except mailer.MailError as exc:
        await log_activity(
            action=Action.ADMIN_SMTP_TEST,
            user=auth.user,
            request=request,
            status="failure",
            detail={"to": recipient, "error": str(exc)},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await log_activity(
        action=Action.ADMIN_SMTP_TEST,
        user=auth.user,
        request=request,
        detail={"to": recipient},
    )
    return JSONResponse({"ok": True, "message": f"测试邮件已发送至 {recipient}"})


# ------------------------------------------------------------------ 存储


@router.get("/storage")
async def storage_info(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    total, used, free = await storage.disk_usage()
    return JSONResponse(
        {
            "ok": True,
            "disk_total": total,
            "disk_used": used,
            "disk_free": free,
            "storage_bytes": await storage.storage_dir_size(),
        }
    )
