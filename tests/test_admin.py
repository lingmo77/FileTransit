"""管理后台：权限门禁、站点设置、用户 CRUD、日志查看 / 导出 / 清空。"""

from __future__ import annotations

import csv
import io

from .conftest import USER_PASSWORD, unique

# ---------------------------------------------------------------- 权限门禁


async def test_admin_api_rejects_guest_and_normal_user(api, user):
    """普通登录用户不是管理员，必须拿到 403 而不是数据。"""
    assert (await api.get("/admin/api/users")).status_code == 403
    assert (await api.get("/admin/api/logs")).status_code == 403
    assert (await api.get("/admin/api/settings")).status_code == 403
    assert (await api.post("/admin/api/maintenance/run")).status_code == 403


async def test_admin_pages_require_admin(api, user):
    for path in ("/admin", "/admin/users", "/admin/logs", "/admin/settings"):
        response = await api.get(path)
        assert response.status_code in (303, 403), f"{path} -> {response.status_code}"


# ---------------------------------------------------------------- 站点设置


async def test_admin_can_change_site_settings_and_it_takes_effect(admin):
    original = (await admin.get("/admin/api/settings")).json()["settings"]

    response = await admin.post(
        "/admin/api/settings",
        json={
            "site.name": "测试中转站",
            "site.default_theme": "dark",
            "registration.enabled": False,
            "quota.default_max_files": 3,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["settings"]
    assert saved["site.name"] == "测试中转站"
    assert saved["registration.enabled"] is False
    assert saved["quota.default_max_files"] == 3

    # 首页标题跟着变
    index = await admin.get("/")
    assert "测试中转站" in index.text

    # 关闭注册后新用户无法注册
    blocked = await admin.register(unique("closed_"), "closed@example.com", USER_PASSWORD)
    assert blocked.status_code == 403

    # 恢复原状，避免影响其它用例
    restore = {
        key: original[key]
        for key in (
            "site.name",
            "site.default_theme",
            "registration.enabled",
            "quota.default_max_files",
        )
    }
    assert (await admin.post("/admin/api/settings", json=restore)).status_code == 200


async def test_admin_settings_rejects_unknown_keys_and_empty_payload(admin):
    assert (await admin.post("/admin/api/settings", json={})).status_code == 400
    assert (
        await admin.post("/admin/api/settings", json={"not.a.real.key": 1})
    ).status_code == 400


async def test_smtp_password_is_never_returned_in_clear(admin):
    response = await admin.post(
        "/admin/api/settings",
        json={
            "email.smtp_host": "smtp.example.com",
            "email.username": "noreply@example.com",
            "email.password": "super-secret-smtp-password",
        },
    )
    assert response.status_code == 200
    body = response.text
    assert "super-secret-smtp-password" not in body
    assert response.json()["settings"]["email.has_password"] is True

    # 数据库里也不能是明文
    from app.core.database import SessionLocal
    from app.models import Setting
    from app.services import settings_service

    async with SessionLocal() as db:
        stored = (await settings_service.all_values(db, force=True))["email.password_enc"]
    assert stored and "super-secret-smtp-password" not in str(stored)

    # 留空再次保存不会清掉已存的密码
    again = await admin.post(
        "/admin/api/settings", json={"email.smtp_host": "smtp.example.com"}
    )
    assert again.status_code == 200
    assert again.json()["settings"]["email.has_password"] is True

    # 派生字段是只读的，不能被请求体塞进设置表
    assert (
        await admin.post("/admin/api/settings", json={"email.has_password": False})
    ).status_code == 400
    async with SessionLocal() as db:
        keys = set(await settings_service.all_values(db, force=True))
    assert "email.has_password" not in keys

    # 收尾：把邮件配置还原成「未配置」，免得影响后面的用例
    assert (
        await admin.post(
            "/admin/api/settings",
            json={"email.smtp_host": "", "email.username": ""},
        )
    ).status_code == 200
    async with SessionLocal() as db:
        row = await db.get(Setting, "email.password_enc")
        row.value = '""'  # 密文字段没有走 API 清除的入口，测试里直接回滚
        await db.commit()
        settings_service.invalidate()
        restored = await settings_service.all_values(db, force=True)
    assert not restored["email.smtp_host"]
    assert not restored["email.password_enc"]


# ---------------------------------------------------------------- 用户管理


async def test_admin_user_crud(admin, transport):
    username = unique("made_")
    created = await admin.post(
        "/admin/api/users",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": USER_PASSWORD,
            "email_verified": True,
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["user"]["id"]

    # 重复用户名
    duplicate = await admin.post(
        "/admin/api/users",
        json={"username": username, "email": "other@example.com", "password": USER_PASSWORD},
    )
    assert duplicate.status_code == 400

    # 改配额与状态
    updated = await admin.patch(
        f"/admin/api/users/{user_id}",
        {"quota_max_files": 2, "quota_max_bytes": 1024 * 1024, "is_active": False},
    )
    assert updated.status_code == 200, updated.text
    payload = updated.json()["user"]
    assert payload["quota_max_files"] == 2
    assert payload["quota_max_bytes"] == 1024 * 1024
    assert payload["is_active"] is False

    # 被禁用的账号无法登录
    import httpx

    from .conftest import Api, BASE_URL

    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        session = Api(client)
        assert (await session.login(username, USER_PASSWORD)).status_code in (401, 403)

    # 重新启用后可以登录
    assert (
        await admin.patch(f"/admin/api/users/{user_id}", {"is_active": True})
    ).status_code == 200
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        session = Api(client)
        response = await session.login(username, USER_PASSWORD)
        assert response.status_code == 200, response.text

    # 空更新被拒绝
    assert (await admin.patch(f"/admin/api/users/{user_id}", {})).status_code == 400

    assert (await admin.delete(f"/admin/api/users/{user_id}")).status_code == 200
    assert (await admin.patch(f"/admin/api/users/{user_id}", {"is_active": True})).status_code == 404


async def test_admin_cannot_delete_self_or_last_admin(admin):
    me = (await admin.get("/api/auth/me")).json()["user"]

    assert (await admin.delete(f"/admin/api/users/{me['id']}")).status_code == 400
    assert (
        await admin.patch(f"/admin/api/users/{me['id']}", {"is_admin": False})
    ).status_code == 400


async def test_admin_can_toggle_permanent_save_permission(admin, user):
    """需求：管理员在用户管理里指定哪些用户可以永久保存文件。"""
    def _row(items):
        return next(u for u in items if u["id"] == user["id"])

    before = _row((await admin.get("/admin/api/users", params={"q": user["username"]})).json()["items"])
    assert before["can_permanent"] is False
    assert before["can_save_forever"] is False

    granted = await admin.patch(f"/admin/api/users/{user['id']}", {"can_permanent": True})
    assert granted.status_code == 200, granted.text
    payload = granted.json()["user"]
    assert payload["can_permanent"] is True
    # 派生字段：前端只看这一个就知道该不该显示「长期有效」选项
    assert payload["can_save_forever"] is True

    # 列表接口也要带回来，否则前端刷新后勾选框会自己弹回去
    listed = _row((await admin.get("/admin/api/users", params={"q": user["username"]})).json()["items"])
    assert listed["can_permanent"] is True

    # 用户管理页要显示徽标，并把当前值带给编辑弹窗
    page = await admin.get("/admin/users", params={"q": user["username"]})
    assert page.status_code == 200, page.text
    assert 'data-permanent="true"' in page.text
    assert "可永久保存" in page.text

    revoked = await admin.patch(f"/admin/api/users/{user['id']}", {"can_permanent": False})
    assert revoked.status_code == 200
    assert revoked.json()["user"]["can_permanent"] is False
    assert revoked.json()["user"]["can_save_forever"] is False


async def test_admin_itself_always_can_save_forever_without_the_flag(admin):
    """管理员不靠这个开关——即使 ``can_permanent`` 是 false，也天然可以永久保存。"""
    me = (await admin.get("/api/auth/me")).json()["user"]
    row = next(
        u for u in (await admin.get("/admin/api/users", params={"q": "admin"})).json()["items"]
        if u["id"] == me["id"]
    )
    assert row["is_admin"] is True
    assert row["can_permanent"] is False
    assert row["can_save_forever"] is True


async def test_admin_can_create_user_already_granted(admin):
    """新建用户时可以直接勾上「允许永久保存」。"""
    username = unique("perm_")
    created = await admin.post(
        "/admin/api/users",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": USER_PASSWORD,
            "email_verified": True,
            "can_permanent": True,
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["user"]["id"]
    assert created.json()["user"]["can_permanent"] is True

    # 不传就是默认不给，别把权限默认放开
    plain_name = unique("plain_")
    plain = await admin.post(
        "/admin/api/users",
        json={
            "username": plain_name,
            "email": f"{plain_name}@example.com",
            "password": USER_PASSWORD,
            "email_verified": True,
        },
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["user"]["can_permanent"] is False

    for uid in (user_id, plain.json()["user"]["id"]):
        assert (await admin.delete(f"/admin/api/users/{uid}")).status_code == 200


async def test_deleting_user_removes_their_files(admin, transport, new_client):
    """需求 3 / 4：用户注销后，其文件随之清理，不留孤儿数据。"""
    from .conftest import create_verified_user

    owner = await new_client()
    info = await create_verified_user(owner, prefix="doomed_")
    uploaded = await owner.upload("doomed.bin", b"bye" * 100)
    assert uploaded.status_code == 201
    file_id = uploaded.json()["file"]["id"]

    assert (await admin.delete(f"/admin/api/users/{info['id']}")).status_code == 200
    assert (await admin.get(f"/admin/api/files/{file_id}")).status_code in (404, 405)


# ---------------------------------------------------------------- 日志


async def test_logs_record_login_ip_browser_and_uploads(admin, api, user):
    """需求 4：日志要能查到登录时间、IP、浏览器信息与上传记录。"""
    uploaded = await api.upload("logged.bin", b"log me" * 10)
    assert uploaded.status_code == 201

    listing = await admin.get(
        "/admin/api/logs", params={"user_id": user["id"], "q": "logged.bin"}
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert items, "上传记录没有写进日志"

    entry = items[0]
    assert entry["action"] == "upload"
    assert entry["username"] == user["username"]
    assert entry["ip_address"]
    assert entry["detail"]

    # 登录日志带浏览器信息
    login = await admin.get(
        "/admin/api/logs", params={"user_id": user["id"], "action": "login"}
    )
    assert login.status_code == 200
    login_entry = login.json()["items"][0]
    assert login_entry["created_at"]
    assert login_entry["ip_address"]
    assert login_entry["browser"]
    assert login_entry["os_name"]


async def test_logs_filters_and_counts(admin):
    unfiltered = (await admin.get("/admin/api/logs")).json()
    assert unfiltered["total"] > 0
    assert isinstance(unfiltered["counts"], dict)

    by_action = (await admin.get("/admin/api/logs", params={"action": "login"})).json()
    assert by_action["total"] >= 1
    assert all(item["action"] == "login" for item in by_action["items"])
    assert by_action["counts"].get("login", 0) >= 1

    # 按 IP 过滤：所有请求都来自同一个测试客户端，所以结果应与不过滤时一致
    by_ip = (await admin.get("/admin/api/logs", params={"ip": "127.0.0.1"})).json()
    assert 0 < by_ip["total"] <= unfiltered["total"]

    assert (await admin.get("/admin/api/logs", params={"q": "不可能存在的关键词"})).json()["total"] == 0


async def test_logs_export_csv(admin):
    response = await admin.get("/admin/api/logs/export")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    text = response.text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    # 时间列按站点时区输出（默认 Asia/Shanghai），不再直接给 UTC
    assert rows[0][0] == "时间(Asia/Shanghai)"
    assert rows[0][1:4] == ["用户", "用户ID", "动作"]
    assert len(rows) > 1


async def test_admin_can_purge_logs_by_range(admin):
    assert (await admin.get("/admin/api/logs")).json()["total"] > 0

    # 只清理 2020 年之前的日志：等于什么都没删
    none = await admin.delete("/admin/api/logs", json={"before": "2020-01-01", "vacuum": False})
    assert none.status_code == 200
    assert none.json()["removed"] == 0

    empty = await admin.delete("/admin/api/logs", json={"action": "不可能的动作"})
    assert empty.status_code == 200
    assert empty.json()["removed"] == 0

    # 非法日期要被挡住
    assert (
        await admin.delete("/admin/api/logs", json={"before": "不是日期"})
    ).status_code == 400

    # 清空全部：清掉此刻存在的所有日志，随后只留下「清空日志」这一条自身记录
    current = (await admin.get("/admin/api/logs")).json()["total"]
    everything = await admin.delete("/admin/api/logs", json={"vacuum": False})
    assert everything.status_code == 200
    assert everything.json()["removed"] == current

    remaining = (await admin.get("/admin/api/logs")).json()["total"]
    assert remaining == 1
    assert (await admin.get("/admin/api/logs")).json()["items"][0]["action"] == "admin_logs_purge"


# ---------------------------------------------------------------- 仪表盘与维护


async def test_admin_stats_and_storage(admin, api, user):
    await api.upload("stats.bin", b"s" * 2048)

    stats = (await admin.get("/admin/api/stats")).json()["stats"]
    assert stats["files_active"] >= 1
    assert stats["files_bytes"] >= 2048
    assert stats["users_total"] >= 1
    assert len(stats["trend"]) == 7

    storage = (await admin.get("/admin/api/storage")).json()
    assert storage["disk_total"] > 0


async def test_admin_dashboard_page_renders_with_files(admin, api, user):
    """回归：库里有文件时后台首页必须渲染得出来。

    模板要显示 ``item.owner.username``，而 ``stats.overview`` 的 recent 查询
    一度漏了 ``selectinload(FileItem.owner)``——异步会话下访问没预加载的关联
    是同步 IO，会抛 MissingGreenlet，整页 500。

    关键在于**必须先传一个文件**：``recent_files`` 为空时模板里那段循环不执行，
    关联根本不会被碰，所以这个 bug 只在有真实数据的库上才暴露，空库测不出来。
    """
    upload = await api.upload("dashboard.bin", b"d" * 512)
    assert upload.status_code == 201, upload.text

    response = await admin.get("/admin")
    assert response.status_code == 200, response.text
    # 「上传者」那一列渲染出来了，说明 owner 关联确实取到了
    assert user["username"] in response.text


async def test_manual_maintenance_run(admin, user):
    from .conftest import expire_task_locks

    await expire_task_locks()
    response = await admin.post("/admin/api/maintenance/run")
    assert response.status_code == 200
    result = response.json()["result"]
    assert set(result) == {"expired_files", "tmp_files", "sessions", "logs"}


async def test_admin_can_take_down_any_file(admin, api, user):
    uploaded = await api.upload("takedown.bin", b"x" * 10)
    file_id = uploaded.json()["file"]["id"]

    response = await admin.patch(
        f"/admin/api/files/{file_id}", {"is_public": False, "expires_hours": 720}
    )
    assert response.status_code == 200, response.text

    assert (await admin.delete(f"/admin/api/files/{file_id}")).status_code == 200
    assert (await admin.get(f"/api/files/{file_id}")).status_code == 404


# ---------------------------------------------------------------- 时区回归


async def test_purge_logs_covers_whole_local_day(admin):
    """回归：填「站点时区的今天」必须清掉当天**全天**的日志。

    改之前清空面板的 ``before`` 漏了 ``end_of_day``，截止点落在当天 00:00，
    当天上午产生的日志清不掉。这里塞一条当天正午（站点时区）的日志来触发。
    时区换算本身由下面的 ``test_parse_date_converts_to_naive_utc`` 把关。
    """
    from datetime import datetime, timezone as dt_timezone

    from sqlalchemy import func, select

    from app.core.database import SessionLocal
    from app.core.timeutil import DEFAULT_TIMEZONE, site_zone
    from app.models import ActivityLog

    zone = site_zone(DEFAULT_TIMEZONE)
    today = datetime.now(zone).strftime("%Y-%m-%d")
    # 本地正午 → UTC；这个时刻在「按 UTC 解释的当天 00:00」之后，能甄别出两种解释
    noon_utc = (
        datetime.strptime(f"{today} 12:00", "%Y-%m-%d %H:%M")
        .replace(tzinfo=zone)
        .astimezone(dt_timezone.utc)
        .replace(tzinfo=None)
    )

    async with SessionLocal() as db:
        db.add(
            ActivityLog(
                username="tz-regression",
                action="login",
                status="success",
                created_at=noon_utc,
            )
        )
        await db.commit()

    async def _left() -> int:
        async with SessionLocal() as db:
            return int(
                (
                    await db.execute(
                        select(func.count(ActivityLog.id)).where(
                            ActivityLog.username == "tz-regression"
                        )
                    )
                ).scalar_one()
            )

    assert await _left() == 1

    # 带 end_of_day：「清空此日期（含当天）及更早」必须覆盖当天全天
    response = await admin.delete(
        "/admin/api/logs", json={"before": today, "vacuum": False}
    )
    assert response.status_code == 200, response.text
    assert await _left() == 0, "站点时区当天的日志没有被清掉"


def test_parse_date_converts_to_naive_utc():
    """纯函数级的时区换算（不依赖跑测试时的挂钟时间）。"""
    from datetime import datetime

    from app.services.logs import parse_date

    # UTC+8 的 2026-10-09 00:00 == UTC 2026-10-08 16:00
    assert parse_date("2026-10-09", tz_name="Asia/Shanghai") == datetime(2026, 10, 8, 16, 0)
    # 末日 23:59:59（本地）== UTC 15:59:59
    assert parse_date(
        "2026-10-09", tz_name="Asia/Shanghai", end_of_day=True
    ) == datetime(2026, 10, 9, 15, 59, 59)
    # 带偏移的 ISO 串按其偏移换算，不能把偏移丢掉
    assert parse_date(
        "2026-10-09T08:00:00+08:00", tz_name="Asia/Shanghai"
    ) == datetime(2026, 10, 9, 0, 0)
    # 不带偏移的按站点时区解释
    assert parse_date(
        "2026-10-09T00:00:00", tz_name="Asia/Shanghai"
    ) == datetime(2026, 10, 8, 16, 0)
    # 同一串在不同站点时区下结果不同
    assert parse_date("2026-10-09", tz_name="UTC") == datetime(2026, 10, 9, 0, 0)
    # 非法 / 空值不抛异常
    assert parse_date("不是日期", tz_name="Asia/Shanghai") is None
    assert parse_date("", tz_name="Asia/Shanghai") is None
    assert parse_date(None, tz_name="Asia/Shanghai") is None
