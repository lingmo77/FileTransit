"""匿名上传：总开关、限额、日志，以及匿名下载记录。

每个用例用自己的客户端 IP（ASGITransport 的 client 参数），互不占用对方的「每个 IP 每天」额度；
用例开始前把匿名上传的设置恢复默认并打开开关、清掉匿名上传日志。
"""
from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.core.database import SessionLocal
from app.models import ActivityLog, FileItem, User
from app.services import anonymous, settings_service
from app.services.activity import Action
from tests.conftest import BASE_URL, Api, count_stored_blobs

ANON_DEFAULTS = {key: value for key, value in settings_service.DEFAULTS.items() if key.startswith("anon.")}


def _ip() -> str:
    return f"198.51.100.{uuid.uuid4().int % 250 + 1}"


@pytest.fixture
async def anon_settings(admin):
    async with SessionLocal() as db:
        await db.execute(delete(ActivityLog).where(ActivityLog.action.in_(
            [Action.ANON_UPLOAD, Action.ANON_UPLOAD_REJECTED])))
        await db.commit()
    anonymous.reset_for_tests()

    async def apply(**values):
        payload = {f"anon.{key}": value for key, value in values.items()}
        response = await admin.post("/admin/api/settings", json=payload)
        assert response.status_code == 200, response.text

    await apply(**{**{key.removeprefix("anon."): value for key, value in ANON_DEFAULTS.items()}, "upload_enabled": True})
    yield apply
    await apply(**{key.removeprefix("anon."): value for key, value in ANON_DEFAULTS.items()})


@pytest.fixture
async def guest(transport):
    """按 IP 创建未登录的访客。"""
    clients: list[httpx.AsyncClient] = []

    async def factory(ip: str | None = None) -> Api:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=transport.app, client=(ip or _ip(), 4321)),
                                   base_url=BASE_URL, follow_redirects=False)
        clients.append(client)
        session = Api(client)
        await session.boot("/")
        return session

    yield factory
    for client in clients:
        await client.aclose()


async def _upload(session: Api, name="anon.txt", content=b"hello anonymous", **params):
    return await session.client.post(
        "/api/anon/files", params={"filename": name, **params}, content=content,
        headers={**session.headers(), "Content-Type": "application/octet-stream",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36"})


async def _logs(action: str, ip: str | None = None) -> list[ActivityLog]:
    async with SessionLocal() as db:
        stmt = select(ActivityLog).where(ActivityLog.action == action).order_by(ActivityLog.id)
        if ip:
            stmt = stmt.where(ActivityLog.ip_address == ip)
        return list((await db.execute(stmt)).scalars())


# ------------------------------------------------------------------ 开关


async def test_disabled_by_default(admin, guest):
    anonymous.reset_for_tests()
    await admin.post("/admin/api/settings", json={"anon.upload_enabled": False})
    ip = _ip()
    visitor = await guest(ip)
    assert "匿名上传" not in (await visitor.get("/")).text
    assert (await visitor.get("/upload")).status_code == 404
    response = await _upload(visitor)
    assert response.status_code == 403 and "未开放匿名上传" in response.json()["error"]
    [log] = await _logs(Action.ANON_UPLOAD_REJECTED, ip)
    assert log.status == "failure" and json.loads(log.detail)["reason"] == "disabled"


async def test_upload_page_and_nav_when_enabled(anon_settings, guest, user, api):
    visitor = await guest()
    assert 'href="/upload"' in (await visitor.get("/")).text
    page = await visitor.get("/upload")
    assert page.status_code == 200 and 'data-endpoint="/api/anon/files"' in page.text
    # 已登录用户直接去「我的文件」
    assert (await api.get("/upload")).headers["location"] == "/dashboard"


# ------------------------------------------------------------------ 上传与日志


async def test_anonymous_upload_is_saved_logged_and_downloadable(anon_settings, guest, admin):
    ip = _ip()
    visitor = await guest(ip)
    response = await _upload(visitor, "报告.txt", b"anonymous content", expires_hours="100")
    assert response.status_code == 201, response.text
    file = response.json()["file"]

    async with SessionLocal() as db:
        item = await db.get(FileItem, file["id"])
        owner = await db.get(User, item.user_id)
        hours = (item.expires_at - item.created_at).total_seconds() / 3600
    assert owner.is_system and owner.username == anonymous.ANONYMOUS_USERNAME
    assert item.uploaded_ip == ip and item.is_public
    assert 23.9 < hours <= 24.01  # 超过上限的保留时长按 24 小时

    [log] = await _logs(Action.ANON_UPLOAD, ip)
    detail = json.loads(log.detail)
    assert log.status == "success" and log.username == "anonymous" and log.user_id is None
    assert log.target_id == str(item.id) and log.path == "/api/anon/files" and log.method == "POST"
    assert log.browser.startswith("Chrome") and log.os_name.startswith("Windows")
    assert detail["filename"] == "报告.txt" and detail["size"] == len(b"anonymous content")
    assert detail["public_id"] == item.public_id and detail["expires_hours"] == 24 and detail["sha256"]

    # 不上首页，分享页显示「匿名用户」
    other = await guest()
    assert item.public_id not in (await other.get("/")).text
    assert "匿名用户" in (await other.get(file["detail_url"])).text

    # 匿名下载有记录
    downloaded = await other.get(file["download_url"], headers={"Referer": "https://example.com/post"})
    assert downloaded.status_code == 200 and downloaded.content == b"anonymous content"
    downloads = [entry for entry in await _logs(Action.DOWNLOAD) if entry.target_id == str(item.id)]
    assert len(downloads) == 1
    detail = json.loads(downloads[0].detail)
    assert downloads[0].username == "anonymous" and downloads[0].user_id is None
    assert detail["filename"] == "报告.txt" and detail["anonymous"] is True and detail["anonymous_file"] is True
    assert detail["referer"] == "https://example.com/post"

    # 后台文件列表能单独筛出匿名上传
    listing = await admin.get(f"/admin/files?user_id={owner.id}")
    assert "匿名用户" in listing.text and ip in listing.text


async def test_logged_in_download_is_logged_with_username(api, user):
    uploaded = await api.upload("mine.txt", b"x" * 10)
    file = uploaded.json()["file"]
    assert (await api.get(file["download_url"])).status_code == 200
    [log] = [entry for entry in await _logs(Action.DOWNLOAD) if entry.target_id == str(file["id"])]
    assert log.username == user["username"] and log.user_id == user["id"]
    assert json.loads(log.detail)["anonymous"] is False


async def test_home_listing_can_include_anonymous_files(anon_settings, guest):
    await anon_settings(list_on_home=True)
    response = await _upload(await guest(), "listed.txt")
    public_id = response.json()["file"]["public_id"]
    assert public_id in (await (await guest()).get("/")).text


# ------------------------------------------------------------------ 限额


async def test_too_large_is_rejected_without_leaving_files(anon_settings, guest):
    await anon_settings(max_upload_mb=1)
    ip = _ip()
    before = count_stored_blobs()
    response = await _upload(await guest(ip), "big.bin", b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert count_stored_blobs() == before
    [log] = await _logs(Action.ANON_UPLOAD_REJECTED, ip)
    assert json.loads(log.detail)["reason"] == "too_large"


async def test_daily_file_limit_per_ip_survives_deletion(anon_settings, guest, admin):
    await anon_settings(ip_daily_files=2)
    ip = _ip()
    visitor = await guest(ip)
    first = await _upload(visitor, "1.txt")
    assert first.status_code == 201
    assert (await _upload(visitor, "2.txt")).status_code == 201
    # 管理员删掉文件，额度也不会回来：统计的是上传记录
    assert (await admin.delete(f"/admin/api/files/{first.json()['file']['id']}")).status_code == 200
    blocked = await _upload(visitor, "3.txt")
    assert blocked.status_code == 429 and "24 小时内最多匿名上传 2 个文件" in blocked.json()["error"]
    # 其他 IP 不受影响
    assert (await _upload(await guest(), "other.txt")).status_code == 201
    [log] = await _logs(Action.ANON_UPLOAD_REJECTED, ip)
    assert json.loads(log.detail)["reason"] == "ip_daily_files"


async def test_daily_bytes_limit_per_ip(anon_settings, guest):
    await anon_settings(ip_daily_mb=1)
    visitor = await guest()
    assert (await _upload(visitor, "a.bin", b"x" * 700_000)).status_code == 201
    blocked = await _upload(visitor, "b.bin", b"x" * 400_000)
    assert blocked.status_code in (413, 429)


async def test_ipv6_addresses_share_a_64_network():
    assert anonymous.limit_key("2001:db8:1:2::5") == anonymous.limit_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert anonymous.limit_key("2001:db8:1:3::5") != anonymous.limit_key("2001:db8:1:2::5")
    assert anonymous.limit_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert anonymous.limit_key("203.0.113.7") == "203.0.113.7"


async def test_ipv6_rotation_inside_64_hits_the_same_limit(anon_settings, guest):
    await anon_settings(ip_daily_files=1)
    assert (await _upload(await guest("2001:db8:aa:bb::1"))).status_code == 201
    assert (await _upload(await guest("2001:db8:aa:bb::2"))).status_code == 429


async def test_site_wide_hourly_limit(anon_settings, guest):
    await anon_settings(hourly_files=1)
    assert (await _upload(await guest())).status_code == 201
    blocked = await _upload(await guest())
    assert blocked.status_code == 429 and "稍后再试" in blocked.json()["error"]


async def test_total_storage_cap(anon_settings, guest):
    async with SessionLocal() as db:
        owner = await db.get(User, await anonymous.system_user_id(db))
        used = owner.used_bytes
    await anon_settings(total_max_mb=used // (1024 * 1024) + 1)
    visitor = await guest()
    response = await _upload(visitor, "fill.bin", b"x" * (1024 * 1024 + 1))
    assert response.status_code == 507 and "空间已满" in response.json()["error"]


async def test_one_upload_at_a_time_per_ip(anon_settings, guest):
    import hashlib

    from app.services.upload_slots import receive_slot

    ip = _ip()
    lock = "anon-ip-" + hashlib.sha256(ip.encode()).hexdigest()[:16]
    with receive_slot(None, 0, groups=[(lock, 1, "busy")]):
        response = await _upload(await guest(ip))
    assert response.status_code == 429 and "同时只能匿名上传一个文件" in response.json()["error"]
    assert (await _upload(await guest(ip))).status_code == 201


# ------------------------------------------------------------------ 拒绝日志


async def test_repeated_rejections_are_collapsed(anon_settings, guest):
    await anon_settings(max_upload_mb=1)
    ip = _ip()
    visitor = await guest(ip)
    for _ in range(3):
        assert (await _upload(visitor, "big.bin", b"x" * (1024 * 1024 + 1))).status_code == 413
    assert len(await _logs(Action.ANON_UPLOAD_REJECTED, ip)) == 1
    # 60 秒后再被拒，补记一条并带上中间省略的次数
    anonymous._recent[(ip, "too_large")][0] = time.monotonic() - 61
    assert (await _upload(visitor, "big.bin", b"x" * (1024 * 1024 + 1))).status_code == 413
    logs = await _logs(Action.ANON_UPLOAD_REJECTED, ip)
    assert len(logs) == 2 and json.loads(logs[1].detail)["suppressed"] == 2


async def test_csrf_is_required(anon_settings, guest):
    visitor = await guest()
    response = await visitor.client.post("/api/anon/files", params={"filename": "x"}, content=b"x",
                                         headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 403


# ------------------------------------------------------------------ 系统账号与后台


async def test_system_account_cannot_sign_in_and_is_hidden(admin, api):
    response = await api.post("/api/auth/login", json={"login": anonymous.ANONYMOUS_USERNAME, "password": "!"})
    assert response.status_code == 401
    response = await api.post("/api/auth/login", json={"login": "anonymous@system.invalid", "password": "!"})
    assert response.status_code == 401
    listing = (await admin.get("/admin/api/users", params={"q": "anonymous"})).json()
    assert all(item["username"] != anonymous.ANONYMOUS_USERNAME for item in listing["items"])
    async with SessionLocal() as db:
        system_id = await anonymous.system_user_id(db)
        assert (await db.execute(select(func.count(User.id)).where(User.is_system.is_(True)))).scalar_one() == 1
    assert (await admin.patch(f"/admin/api/users/{system_id}", {"is_active": False})).status_code == 404
    assert (await admin.delete(f"/admin/api/users/{system_id}")).status_code == 404


@pytest.mark.parametrize(("payload", "ok"), [
    ({"anon.max_expire_hours": 25}, False),
    ({"anon.max_expire_hours": 0}, False),
    ({"anon.ip_daily_files": -1}, False),
    ({"anon.upload_enabled": "yes"}, False),
    ({"anon.max_upload_mb": 1.5}, False),
    ({"anon.max_expire_hours": 12}, True),
])
async def test_admin_settings_are_validated(admin, payload, ok):
    response = await admin.post("/admin/api/settings", json=payload)
    assert (response.status_code == 200) is ok, response.text
    if ok:
        await admin.post("/admin/api/settings", json={"anon.max_expire_hours": 24})
