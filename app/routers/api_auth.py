"""认证相关的 JSON 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ..core.config import settings
from ..core.database import get_db
from ..core.deps import AuthContext, check_csrf, get_auth, require_user
from ..core.security import (
    client_ip,
    login_limiter,
    register_limiter,
    resend_limiter,
    user_agent,
    verify_password,
)
from ..services import account
from ..services import auth as auth_service
from ..services import mailer, settings_service
from ..services.activity import Action, log_activity
from .pages import safe_next

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    username: str = Field(max_length=64)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class LoginIn(BaseModel):
    login: str = Field(max_length=254)
    password: str = Field(max_length=128)
    next: str | None = None


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(max_length=256)
    password: str = Field(max_length=128)


class ChangePasswordIn(BaseModel):
    current: str = Field(max_length=128)
    password: str = Field(max_length=128)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


# ------------------------------------------------------------------ 注册


@router.post("/register")
async def register(
    payload: RegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    if not await settings_service.get_bool(db, "registration.enabled", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本站当前已关闭注册")

    ip = client_ip(request)
    if not register_limiter.check(ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "注册过于频繁，请稍后再试"
        )

    try:
        user, mail_sent = await account.register(
            db,
            username=payload.username,
            email=payload.email,
            password=payload.password,
            request=request,
        )
    except account.AccountError as exc:
        await log_activity(
            action=Action.REGISTER,
            request=request,
            status="failure",
            username=payload.username,
            detail={"reason": exc.message},
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    await log_activity(
        action=Action.REGISTER,
        user=user,
        request=request,
        target_type="user",
        target_id=user.id,
        detail={"email": user.email, "mail_sent": mail_sent},
    )

    if not await settings_service.get_bool(db, "registration.require_email_verification", True):
        raw = await auth_service.create_session(
            db, user, ip=ip, user_agent=user_agent(request)
        )
        response = JSONResponse(
            {"ok": True, "redirect": "/dashboard", "message": "注册成功"}
        )
        _set_session_cookie(response, raw)
        return response

    return JSONResponse(
        {
            "ok": True,
            "redirect": "/login",
            "message": (
                "注册成功，请前往邮箱完成验证"
                if mail_sent
                else "账号已创建，但验证邮件发送失败，请在登录页重新发送"
            ),
            "mail_sent": mail_sent,
        }
    )


@router.post("/resend-verification")
async def resend_verification(
    payload: EmailIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    if not resend_limiter.check(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "操作过于频繁，请稍后再试")

    user = await auth_service.find_user_by_email(db, payload.email)
    generic = {"ok": True, "message": "如果该邮箱存在且未验证，验证邮件已重新发送"}
    if user is None or user.email_verified or not user.is_active:
        return JSONResponse(generic)

    sent = await account.send_verification(db, user, request=request)
    await log_activity(
        action=Action.RESEND_VERIFICATION,
        user=user,
        request=request,
        status="success" if sent else "failure",
    )
    if not sent:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "邮件发送失败，请联系管理员检查邮件配置")
    return JSONResponse(generic)


# ------------------------------------------------------------------ 登录 / 登出


@router.post("/login")
async def login(
    payload: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    ip = client_ip(request)
    if not login_limiter.check(f"{ip}:{payload.login.lower()}"):
        await log_activity(
            action=Action.LOGIN_FAILED,
            request=request,
            username=payload.login,
            status="failure",
            detail={"reason": "too_many_attempts"},
        )
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "尝试次数过多，请 5 分钟后再试")

    user = await auth_service.find_user_by_login(db, payload.login)
    # bcrypt 校验较慢，放到线程池避免阻塞事件循环
    ok = user is not None and await run_in_threadpool(
        verify_password, payload.password, user.password_hash
    )

    if not ok:
        await log_activity(
            action=Action.LOGIN_FAILED,
            request=request,
            username=payload.login,
            status="failure",
            detail={"reason": "bad_credentials"},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码不正确")

    assert user is not None

    if not user.is_active:
        await log_activity(
            action=Action.LOGIN_FAILED,
            user=user,
            request=request,
            status="failure",
            detail={"reason": "disabled"},
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")

    if not user.email_verified and await settings_service.get_bool(
        db, "registration.require_email_verification", True
    ):
        await log_activity(
            action=Action.LOGIN_FAILED,
            user=user,
            request=request,
            status="failure",
            detail={"reason": "email_unverified"},
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "邮箱尚未验证，请先完成邮箱验证")

    raw = await auth_service.create_session(db, user, ip=ip, user_agent=user_agent(request))
    login_limiter.reset(f"{ip}:{payload.login.lower()}")

    target = safe_next(payload.next, "/dashboard")
    if user.must_change_password:
        target = "/me/settings?force_password=1"

    await log_activity(action=Action.LOGIN, user=user, request=request, target_type="user", target_id=user.id)

    response = JSONResponse({"ok": True, "redirect": target, "message": "登录成功"})
    _set_session_cookie(response, raw)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    raw = request.cookies.get(settings.session_cookie_name)
    await auth_service.revoke_session(db, raw)
    if auth is not None:
        await log_activity(action=Action.LOGOUT, user=auth.user, request=request)

    response = JSONResponse({"ok": True, "redirect": "/"})
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


# ------------------------------------------------------------------ 找回 / 修改密码


@router.post("/forgot")
async def forgot(
    payload: EmailIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    if not resend_limiter.check(f"forgot:{client_ip(request)}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "操作过于频繁，请稍后再试")

    try:
        await account.request_password_reset(db, payload.email, request=request)
    except account.AccountError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc

    await log_activity(
        action=Action.PASSWORD_CHANGE,
        request=request,
        username=payload.email,
        detail={"stage": "request_reset"},
    )
    return JSONResponse(
        {"ok": True, "message": "如果该邮箱已注册，重置链接已发送，请查收"}
    )


@router.post("/reset")
async def reset(
    payload: ResetIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    try:
        user = await account.reset_password(db, payload.token, payload.password)
    except account.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "重置链接无效或已过期")

    await log_activity(
        action=Action.PASSWORD_CHANGE, user=user, request=request, detail={"stage": "reset"}
    )
    return JSONResponse({"ok": True, "redirect": "/login", "message": "密码已重置，请重新登录"})


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    raw = request.cookies.get(settings.session_cookie_name)
    try:
        await account.change_password(
            db,
            auth.user,
            current=payload.current,
            new=payload.password,
            keep_session_token=raw,
        )
    except account.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    await log_activity(action=Action.PASSWORD_CHANGE, user=auth.user, request=request)
    return JSONResponse({"ok": True, "message": "密码已更新，其它设备已下线"})


@router.post("/update-profile")
async def update_profile(
    payload: EmailIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(require_user),
    _csrf: None = Depends(check_csrf),
) -> JSONResponse:
    """修改邮箱：改完需要重新验证。"""
    email = account.validate_email(payload.email)
    if email == auth.user.email:
        return JSONResponse({"ok": True, "message": "邮箱未变化"})

    existing = await auth_service.find_user_by_email(db, email)
    if existing is not None and existing.id != auth.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该邮箱已被其它账号使用")

    auth.user.email = email
    auth.user.email_verified = False
    await db.commit()

    sent = await account.send_verification(db, auth.user, request=request)
    await log_activity(
        action=Action.PROFILE_UPDATE,
        user=auth.user,
        request=request,
        detail={"email": email, "mail_sent": sent},
    )
    return JSONResponse(
        {
            "ok": True,
            "message": "邮箱已更新，请查收验证邮件"
            if sent
            else "邮箱已更新，但验证邮件发送失败，请稍后重发",
        }
    )


@router.get("/me")
async def me(auth: AuthContext = Depends(require_user)) -> JSONResponse:
    user = auth.user
    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_admin": user.is_admin,
                "email_verified": user.email_verified,
                "used_files": user.used_files,
                "used_bytes": user.used_bytes,
            },
        }
    )


__all__ = ["router", "mailer"]
