"""测试夹具。

测试永远跑在临时数据目录里，不会碰到 ``data/`` 下的真实数据。
环境变量必须在导入 ``app`` 之前设置好（配置是模块级读取的）。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid

import pytest

# --------------------------------------------------------------- 环境隔离

_TEST_DIR = tempfile.mkdtemp(prefix="ft-test-")
os.environ["FT_DATA_DIR"] = _TEST_DIR
os.environ["FT_SECRET_KEY"] = "test-secret-key-0123456789abcdef"
os.environ["FT_COOKIE_SECURE"] = "false"
os.environ["FT_ADMIN_USERNAME"] = "admin"
os.environ["FT_ADMIN_PASSWORD"] = "admin"
os.environ["FT_SESSION_TTL_HOURS"] = "24"
# 所有请求都来自同一个 IP，限流会互相干扰；限流本身由 test_security.py 单测覆盖
os.environ["FT_RATE_LIMIT_ENABLED"] = "false"

import httpx  # noqa: E402

BASE_URL = "http://testserver"
_CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')

#: 种子管理员口令（需求 7），也用于断言默认口令可登录
ADMIN_PASSWORD = "admin"
USER_PASSWORD = "user-password-123"


def unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


class Api:
    """带 CSRF 处理与常用动作的小客户端封装。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.csrf: str | None = None

    # -- 基础

    async def boot(self, path: str = "/login") -> httpx.Response:
        response = await self.client.get(path)
        match = _CSRF_RE.search(response.text)
        if match:
            self.csrf = match.group(1)
        return response

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, *, json=None, data=None, files=None, csrf: bool = True):
        headers = self.headers() if csrf else {}
        return await self.client.post(url, json=json, data=data, files=files, headers=headers)

    async def patch(self, url: str, payload: dict):
        return await self.client.patch(url, json=payload, headers=self.headers())

    async def delete(self, url: str, *, json=None):
        return await self.client.request(
            "DELETE", url, json=json, headers=self.headers()
        )

    # -- 业务动作

    async def login(self, login: str, password: str) -> httpx.Response:
        await self.boot()
        return await self.post(
            "/api/auth/login", json={"login": login, "password": password}
        )

    async def logout(self) -> httpx.Response:
        return await self.post("/api/auth/logout")

    async def register(self, username: str, email: str, password: str) -> httpx.Response:
        await self.boot()
        return await self.post(
            "/api/auth/register",
            json={"username": username, "email": email, "password": password},
        )

    async def upload(
        self,
        name: str = "demo.bin",
        content: bytes = b"0123456789" * 8,
        *,
        expires_hours: int = 24,
        is_public: bool = True,
    ) -> httpx.Response:
        return await self.client.post(
            "/api/files",
            files={"file": (name, content, "application/octet-stream")},
            data={
                "expires_hours": str(expires_hours),
                "is_public": "true" if is_public else "false",
            },
            headers=self.headers(),
        )


# --------------------------------------------------------------- 夹具


@pytest.fixture(scope="session")
async def transport():
    from app.core.bootstrap import bootstrap
    from app.core.config import settings

    settings.ensure_dirs()
    # ASGITransport 不会触发 lifespan，这里手动完成建表与种子数据
    await asyncio.to_thread(bootstrap)

    from app.main import app

    yield httpx.ASGITransport(app=app)

    from app.core.database import engine

    await engine.dispose()
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


@pytest.fixture
async def api(transport):
    """一个独立的访客会话（独立 Cookie）。"""
    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL, follow_redirects=False
    ) as client:
        session = Api(client)
        await session.boot()
        yield session


@pytest.fixture(scope="session")
async def admin(transport):
    """已登录的管理员会话（仍然是种子口令 ``admin/admin``）。

    默认管理员带 ``must_change_password`` 标记，页面访问会被重定向到个人设置。
    这里只把这个标记清掉（改密接口本身由 ``test_auth`` 覆盖），
    免得每个后台用例都要先绕一圈改密流程。
    """
    from sqlalchemy import select

    from app.core.config import settings as env_settings
    from app.core.database import SessionLocal
    from app.models import User

    async with SessionLocal() as db:
        row = await db.execute(
            select(User).where(User.username == env_settings.admin_username)
        )
        row.scalar_one().must_change_password = False
        await db.commit()

    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL, follow_redirects=False
    ) as client:
        session = Api(client)
        await session.boot()

        response = await session.post(
            "/api/auth/login", json={"login": "admin", "password": "admin"}
        )
        assert response.status_code == 200, response.text

        yield session


@pytest.fixture
async def new_client(transport):
    """按需创建额外的独立会话（例如「另一台设备」或游客）。"""
    created: list[httpx.AsyncClient] = []

    async def factory() -> Api:
        client = httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, follow_redirects=False
        )
        created.append(client)
        session = Api(client)
        await session.boot()
        return session

    yield factory

    for client in created:
        await client.aclose()


async def expire_task_locks() -> None:
    """把任务锁的 TTL 拨到过去。

    维护任务的锁只靠 TTL 释放（不做显式解锁），测试里想连续触发两次维护就得先放开它。
    """
    from datetime import timedelta

    from sqlalchemy import update

    from app.core.database import SessionLocal
    from app.models import TaskLock, utcnow

    async with SessionLocal() as db:
        await db.execute(
            update(TaskLock).values(locked_until=utcnow() - timedelta(minutes=1))
        )
        await db.commit()


def count_stored_blobs() -> int:
    """统计存储目录里实际落盘的文件个数。

    用来抓「请求被拒但文件已经写进磁盘」这类孤儿文件——数据库里查不到，
    只能直接看目录。
    """
    from app.core.config import settings

    return sum(1 for p in settings.storage_dir.rglob("*") if p.is_file())


def stored_blob_bytes() -> int:
    """存储目录里所有文件的总字节数，用于比对是否多出一次上传。"""
    from app.core.config import settings

    return sum(p.stat().st_size for p in settings.storage_dir.rglob("*") if p.is_file())


async def create_verified_user(api: Api, prefix: str = "user_") -> dict[str, object]:
    """注册一个用户并跳过邮件验证（测试环境没有 SMTP）。"""
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import User

    username = unique(prefix)
    password = USER_PASSWORD

    response = await api.register(username, f"{username}@example.com", password)
    assert response.status_code == 200, response.text

    async with SessionLocal() as db:
        row = await db.execute(select(User).where(User.username == username))
        account = row.scalar_one()
        account.email_verified = True
        await db.commit()
        user_id = account.id

    response = await api.login(username, password)
    assert response.status_code == 200, response.text

    return {"id": user_id, "username": username, "password": password}


@pytest.fixture
async def user(api):
    """一个邮箱已验证、已登录的普通用户。"""
    return await create_verified_user(api)
