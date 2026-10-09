"""下载路由。

关键目标：让 IDM / 迅雷 / aria2 这类下载器能并发分片抓取。
为此必须满足：

* 返回 ``Accept-Ranges: bytes``；
* 完整支持 ``Range`` 请求（``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n``），
  由 Starlette 的 ``FileResponse`` 处理并返回 ``206`` + ``Content-Range``；
* 支持 ``HEAD``（下载器先探测大小）；
* 二进制响应不做 gzip，避免 ``Content-Length`` 与实际字节数不一致；
* ``ETag`` 必须是合法的 opaque-tag（见 ``_build_response``），否则下载器回传
  ``If-Range`` 时对不上，Starlette 会退回 ``200`` 整包，分片连接被客户端掐断；
* 每个请求独立打开文件句柄，不加任何进程内锁，连接数由反向代理与系统决定。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..core.database import SessionLocal, get_db
from ..core.deps import AuthContext, get_auth
from ..core.security import client_ip, user_agent
from ..models import FileItem
from ..services import files as files_service
from ..services import settings_service, storage
from ..services.activity import Action, log_activity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["download"])

#: 仅当分片从第 0 字节开始时计一次下载，否则一次多线程下载会被算成几十次
_RANGE_START = re.compile(r"^\s*bytes=(\d+)\s*-")

#: 这些类型即使以附件形式返回也建议降级，避免浏览器嗅探后内联执行
_FORCE_OCTET_STREAM = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "application/javascript",
        "text/javascript",
        "text/x-sh",
    }
)


async def _resolve_item(
    db: AsyncSession, public_id: str, auth: AuthContext | None
) -> tuple[FileItem, bool]:
    item = await files_service.get_by_public_id(db, public_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或已被删除")

    is_owner = auth is not None and (auth.user.id == item.user_id or auth.user.is_admin)
    if not item.is_public and not is_owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在或已被删除")
    if item.is_expired:
        raise HTTPException(status.HTTP_410_GONE, "文件已过期，已无法下载")

    if not is_owner and auth is None:
        allowed = await settings_service.get_bool(db, "site.allow_guest_download", True)
        if not allowed:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "本站已关闭游客下载，请先登录")

    return item, is_owner


def _should_count(request: Request) -> bool:
    if request.method != "GET":
        return False
    range_header = request.headers.get("range")
    if not range_header:
        return True
    match = _RANGE_START.match(range_header)
    return bool(match and match.group(1) == "0")


async def _count_download(item_id: int, *, ip: str | None, ua: str | None) -> None:
    """响应体发完之后再记账，避免给首个字节增加延迟。"""
    async with SessionLocal() as db:
        await db.execute(
            update(FileItem)
            .where(FileItem.id == item_id)
            .values(download_count=FileItem.download_count + 1)
        )
        await db.commit()

        if not await settings_service.get_bool(db, "site.log_anonymous_download", True):
            return

    await log_activity(
        action=Action.DOWNLOAD,
        request=None,
        username="anonymous",
        ip=ip,
        ua=ua,
        target_type="file",
        target_id=item_id,
    )


async def _build_response(
    request: Request, item: FileItem, *, user_id: int | None
) -> FileResponse:
    try:
        path = storage.resolve(item.stored_path)
    except storage.StorageError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件路径非法") from None

    if not path.is_file():
        logger.error("数据库记录存在但文件缺失: %s", item.stored_path)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件已不在服务器上")

    media_type = (item.mime_type or "application/octet-stream").lower()
    if media_type in _FORCE_OCTET_STREAM:
        media_type = "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        # 文件内容不可变（重名会换 public_id），可以放心长缓存
        "Cache-Control": "public, max-age=31536000, immutable",
        # ETag 只能是 RFC 7232 的 opaque-tag：双引号内允许的字符集（etagc）
        # 排除空格、双引号和逗号。这里只放 public_id 与 size，两者都只含
        # [A-Za-z0-9_-] 和数字，天然合法；同一个 public_id 的内容永不改变，
        # 改有效期/改文件名都不影响字节，所以不带 expires_at 也是正确的。
        #
        # 别把 expires_at 拼进来：datetime 的 str() 形如
        # "2026-10-12 03:11:22.014865"，中间那个空格就是非法字符。下载器
        # （IDM / 迅雷 / aria2）拿到非法 ETag 后回传的 If-Range 很可能对不上，
        # 而 Starlette 的 FileResponse 只要 If-Range 匹配失败就会返回 200 整包
        # 而不是 206——分片连接收到整包只能断开，表现为"多线程下载连不上第二条"。
        "ETag": f'"{item.public_id}-{item.size}"',
    }

    background = None
    if _should_count(request):
        background = BackgroundTask(
            _count_download,
            item.id,
            ip=client_ip(request),
            ua=user_agent(request)[:512] or None,
        )

    return FileResponse(
        path,
        media_type=media_type,
        filename=item.original_name,
        headers=headers,
        stat_result=path.stat(),
        background=background,
    )


@router.api_route("/d/{public_id}", methods=["GET", "HEAD"], name="download_direct")
async def download_direct(
    public_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> FileResponse:
    item, _ = await _resolve_item(db, public_id, auth)
    return await _build_response(request, item, user_id=auth.user.id if auth else None)


@router.api_route(
    "/download/{public_id}/{filename}", methods=["GET", "HEAD"], name="download_named"
)
async def download_named(
    public_id: str,
    filename: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext | None = Depends(get_auth),
) -> FileResponse:
    """带文件名后缀的直链，方便下载器识别类型。

    URL 里的 ``filename`` 只是给下载器看的装饰，真正的文件名一律取自数据库。
    """
    item, _ = await _resolve_item(db, public_id, auth)
    return await _build_response(request, item, user_id=auth.user.id if auth else None)
