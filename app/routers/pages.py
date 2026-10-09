"""页面路由：首页、文件详情、登录注册、用户中心。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.deps import AuthContext, get_auth, page_context, require_user_page
from ..core.templating import CATEGORY_LABELS, templates
from ..models import ActivityLog, UserSession, utcnow
from ..services import account
from ..services import files as files_service
from ..services import quota as quota_service
from ..services import settings_service
from ..services.activity import Action, log_activity

router = APIRouter()


def safe_next(value: str | None, default: str = "/dashboard") -> str:
    """只允许站内跳转，避免开放重定向。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    return value


# ------------------------------------------------------------------ 首页


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    kind: str = Query("", max_length=16),
) -> HTMLResponse:
    result = await files_service.list_public(
        db,
        page=page,
        q=q.strip() or None,
        kind=kind.strip() or None,
    )
    total_files, total_bytes = await files_service.public_summary(db)

    context = await page_context(
        request,
        db,
        auth,
        result=result,
        q=q,
        kind=kind,
        categories=CATEGORY_LABELS,
        total_files=total_files,
        total_bytes=total_bytes,
    )
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/f/{public_id}", response_class=HTMLResponse)
async def file_detail(
    public_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> HTMLResponse:
    item = await files_service.get_by_public_id(db, public_id)
    if item is None:
        raise HTTPException(404, "文件不存在或已被删除")

    is_owner = auth is not None and (auth.user.id == item.user_id or auth.user.is_admin)
    if not item.is_public and not is_owner:
        raise HTTPException(404, "文件不存在或已被删除")

    expired = item.is_expired
    context = await page_context(
        request,
        db,
        auth,
        item=item,
        is_owner=is_owner,
        expired=expired,
        download_url=f"/download/{item.public_id}/{item.original_name}",
    )
    return templates.TemplateResponse(request, "file_detail.html", context)


# ------------------------------------------------------------------ 登录 / 注册


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
    next: str = Query("/dashboard"),
) -> HTMLResponse:
    if auth is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    context = await page_context(request, db, auth, next_url=safe_next(next), mode="login")
    return templates.TemplateResponse(request, "auth.html", context)


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> HTMLResponse:
    if auth is not None:
        return RedirectResponse("/dashboard", status_code=303)

    context = await page_context(request, db, auth, mode="register")
    enabled = await settings_service.get_bool(db, "registration.enabled", True)
    context["registration_enabled"] = enabled
    return templates.TemplateResponse(request, "auth.html", context)


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> HTMLResponse:
    context = await page_context(request, db, auth, mode="forgot")
    return templates.TemplateResponse(request, "auth.html", context)


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(
    request: Request,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> HTMLResponse:
    user = await account.verify_email(db, token) if token else None
    if user is not None:
        await log_activity(
            action=Action.EMAIL_VERIFIED,
            user=user,
            request=request,
            target_type="user",
            target_id=user.id,
        )

    context = await page_context(
        request,
        db,
        auth,
        success=user is not None,
        username=user.username if user else "",
        message=(
            "邮箱验证成功，现在可以登录了。"
            if user
            else "验证链接无效或已过期，请登录后重新发送验证邮件。"
        ),
    )
    return templates.TemplateResponse(request, "verify.html", context)


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(
    request: Request,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> HTMLResponse:
    context = await page_context(request, db, auth, mode="reset", token=token)
    return templates.TemplateResponse(request, "auth.html", context)


# ------------------------------------------------------------------ 用户中心


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user_page),
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=120),
    kind: str = Query("", max_length=16),
) -> HTMLResponse:
    result = await files_service.list_for_user(
        db, auth.user, page=page, q=q.strip() or None, kind=kind.strip() or None
    )
    quota = await quota_service.resolve_quota(db, auth.user)
    cfg = await settings_service.template_settings(db)

    context = await page_context(
        request,
        db,
        auth,
        result=result,
        quota=quota,
        categories=CATEGORY_LABELS,
        q=q,
        kind=kind,
        max_expire_days=cfg["max_expire_days"],
        max_upload_mb=cfg["max_upload_mb"],
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/me/settings", response_class=HTMLResponse)
async def my_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user_page),
    force_password: int = Query(0),
) -> HTMLResponse:
    user = auth.user
    sessions = (
        (
            await db.execute(
                select(UserSession)
                .where(
                    UserSession.user_id == user.id,
                    UserSession.revoked.is_(False),
                    UserSession.expires_at > utcnow(),
                )
                .order_by(UserSession.last_seen_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    logs = (
        (
            await db.execute(
                select(ActivityLog)
                .where(ActivityLog.user_id == user.id)
                .order_by(ActivityLog.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    quota = await quota_service.resolve_quota(db, user)

    context = await page_context(
        request,
        db,
        auth,
        sessions=sessions,
        logs=logs,
        quota=quota,
        force_password=bool(force_password or user.must_change_password),
    )
    return templates.TemplateResponse(request, "settings.html", context)
