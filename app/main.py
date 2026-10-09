"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.bootstrap import bootstrap
from .core.config import settings
from .core.deps import CSRF_COOKIE, AdminRequired, LoginRequired, PasswordChangeRequired
from .core.templating import STATIC_DIR, templates
from .routers import admin, admin_pages, api_auth, download, files, pages
from .services import cleanup

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    await run_in_threadpool(bootstrap)
    cleanup.start_scheduler()
    logger.info("%s 已启动（数据目录 %s）", settings.app_name, settings.data_dir)
    try:
        yield
    finally:
        cleanup.stop_scheduler()
        logger.info("已停止")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    # 生产环境不暴露交互式文档，避免泄露内部接口结构
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.debug else None,
)


# ------------------------------------------------------------------ 中间件


@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    """双提交 Cookie：为每个访客准备一枚 CSRF 令牌。

    未登录用户也要提交注册/登录表单，所以令牌必须与登录态解耦。
    """
    token = request.cookies.get(CSRF_COOKIE)
    fresh = not token
    if fresh:
        token = secrets.token_urlsafe(24)
    request.state.csrf_token = token

    response = await call_next(request)
    if fresh:
        response.set_cookie(
            CSRF_COOKIE,
            token,
            max_age=settings.session_ttl_hours * 3600,
            httponly=False,  # 前端 JS 需要读取后放进请求头
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ------------------------------------------------------------------ 异常处理


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/api/") or path.startswith("/admin/api/")


@app.exception_handler(LoginRequired)
async def _login_required_handler(request: Request, exc: LoginRequired):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)

    target = "/login"
    if exc.next_url and exc.next_url not in {"/", "/login"}:
        target = f"/login?next={quote(exc.next_url, safe='/')}"
    return RedirectResponse(target, status_code=303)


@app.exception_handler(PasswordChangeRequired)
async def _password_change_handler(request: Request, _exc: PasswordChangeRequired):
    if _wants_json(request):
        return JSONResponse(
            {"ok": False, "error": "请先修改默认密码", "redirect": "/me/settings?force_password=1"},
            status_code=403,
        )
    return RedirectResponse("/me/settings?force_password=1", status_code=303)


@app.exception_handler(AdminRequired)
async def _admin_required_handler(request: Request, exc: AdminRequired):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": exc.message}, status_code=403)
    return await _render_error(request, 403, exc.message)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
    return await _render_error(request, exc.status_code, str(exc.detail))


async def _render_error(request: Request, code: int, message: str) -> Response:
    from .core.database import SessionLocal
    from .core.deps import get_auth, page_context

    async with SessionLocal() as db:
        try:
            auth = await get_auth(request, db)
        except Exception:  # noqa: BLE001 - 错误页不应因为取用户失败而挂掉
            auth = None
        context = await page_context(request, db, auth)
    context.update({"code": code, "message": message})
    return templates.TemplateResponse(request, "error.html", context, status_code=code)


# ------------------------------------------------------------------ 基础路由


@app.get("/healthz", include_in_schema=False)
async def healthz() -> Response:
    return JSONResponse({"status": "ok", "app": settings.app_name, "version": app.version})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return Response(
            icon.read_bytes(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    return Response(status_code=204)


app.include_router(pages.router)
app.include_router(api_auth.router)
app.include_router(files.router)
app.include_router(files.me_router)
app.include_router(download.router)
app.include_router(admin.router)
app.include_router(admin_pages.router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
