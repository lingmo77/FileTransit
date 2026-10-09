"""管理员后台页面。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.deps import AuthContext, page_context, require_admin_page
from ..core.templating import templates
from ..services import files as files_service
from ..services import logs as logs_service
from ..services import settings_service, stats
from ..services import users as users_service

router = APIRouter(prefix="/admin", tags=["admin-pages"])


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
) -> HTMLResponse:
    data = await stats.overview(db)
    context = await page_context(request, db, auth, section="dashboard", **data)
    return templates.TemplateResponse(request, "admin/dashboard.html", context)


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    status_filter: str = Query("", alias="status", max_length=16),
) -> HTMLResponse:
    result = await users_service.list_users(
        db, page=page, q=q.strip() or None, status=status_filter.strip() or None
    )
    cfg = await settings_service.template_settings(db)
    context = await page_context(
        request,
        db,
        auth,
        section="users",
        result=result,
        q=q,
        status=status_filter,
        cfg=cfg,
    )
    return templates.TemplateResponse(request, "admin/users.html", context)


@router.get("/files", response_class=HTMLResponse)
async def files_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    user_id: int | None = Query(None),
    expired: int = Query(0),
) -> HTMLResponse:
    result = await files_service.list_admin(
        db, page=page, q=q.strip() or None, user_id=user_id, expired=bool(expired)
    )
    context = await page_context(
        request,
        db,
        auth,
        section="files",
        result=result,
        q=q,
        user_id=user_id,
        expired=bool(expired),
    )
    return templates.TemplateResponse(request, "admin/files.html", context)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    action: str = Query("", max_length=48),
    status_filter: str = Query("", alias="status", max_length=16),
    user_id: int | None = Query(None),
    ip: str = Query("", max_length=64),
    start: str = Query("", max_length=32),
    end: str = Query("", max_length=32),
) -> HTMLResponse:
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
    counts = await logs_service.action_counts(db)
    context = await page_context(
        request,
        db,
        auth,
        section="logs",
        result=result,
        counts=counts,
        action_labels=logs_service.ACTION_LABELS,
        filters={
            "q": q,
            "action": action,
            "status": status_filter,
            "user_id": user_id or "",
            "ip": ip,
            "start": start,
            "end": end,
        },
    )
    return templates.TemplateResponse(request, "admin/logs.html", context)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
) -> HTMLResponse:
    values = await settings_service.all_values(db, force=True)
    values = {k: v for k, v in values.items() if k not in settings_service.SECRET_KEYS}
    values["email.has_password"] = bool(
        (await settings_service.all_values(db)).get("email.password_enc")
    )
    context = await page_context(request, db, auth, section="settings", values=values)
    return templates.TemplateResponse(request, "admin/settings.html", context)


@router.get("/email", response_class=HTMLResponse)
async def email_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_admin_page),
) -> HTMLResponse:
    raw = await settings_service.all_values(db, force=True)
    values = {k: v for k, v in raw.items() if k not in settings_service.SECRET_KEYS}
    values["email.has_password"] = bool(raw.get("email.password_enc"))
    context = await page_context(request, db, auth, section="email", values=values)
    return templates.TemplateResponse(request, "admin/email.html", context)
