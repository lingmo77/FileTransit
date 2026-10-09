"""注册、登录、CSRF、改密。"""

from __future__ import annotations

from .conftest import unique


async def test_login_with_default_admin(api):
    response = await api.login("admin", "admin")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "ft_session" in response.cookies or "ft_session" in api.client.cookies


async def test_login_rejects_wrong_password(api):
    response = await api.login("admin", "not-the-password")
    assert response.status_code == 401
    assert "密码" in response.json()["error"]


async def test_post_without_csrf_header_is_blocked(api):
    await api.boot()
    response = await api.post(
        "/api/auth/login",
        json={"login": "admin", "password": "admin"},
        csrf=False,
    )
    assert response.status_code == 403


async def test_csrf_header_must_match_cookie(api):
    await api.boot()
    response = await api.client.post(
        "/api/auth/login",
        json={"login": "admin", "password": "admin"},
        headers={"X-CSRF-Token": "forged-token"},
    )
    assert response.status_code == 403


async def test_register_then_login(api):
    username = unique("reg_")
    password = "register-pass-123"

    response = await api.register(username, f"{username}@example.com", password)
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 该用户邮箱尚未验证，且站点默认要求验证，因此不能登录
    response = await api.login(username, password)
    assert response.status_code == 403

    # 用户名重复
    response = await api.register(username, f"{username}2@example.com", password)
    assert response.status_code == 400

    # 弱密码
    response = await api.register(unique("weak_"), "weak@example.com", "123")
    assert response.status_code == 400


async def test_logout_invalidates_session(user, api):
    response = await api.get("/dashboard")
    assert response.status_code == 200

    response = await api.logout()
    assert response.status_code == 200

    response = await api.get("/dashboard")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_change_password_and_revoke_other_sessions(api, user, transport):
    response = await api.post(
        "/api/auth/change-password",
        json={"current": "wrong", "password": "another-pass-123"},
    )
    assert response.status_code == 400

    response = await api.post(
        "/api/auth/change-password",
        json={"current": user["password"], "password": user["password"]},
    )
    assert response.status_code == 400  # 新旧密码不能相同

    # 换一个「设备」登录，稍后应被强制下线
    import httpx

    from .conftest import Api, BASE_URL

    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as other_client:
        other = Api(other_client)
        assert (await other.login(user["username"], user["password"])).status_code == 200
        assert (await other.get("/dashboard")).status_code == 200

        response = await api.post(
            "/api/auth/change-password",
            json={"current": user["password"], "password": "brand-new-pass-456"},
        )
        assert response.status_code == 200

        # 当前会话保留，其它设备被下线，旧密码失效
        assert (await api.get("/dashboard")).status_code == 200
        assert (await other.get("/dashboard")).status_code == 303
        assert (await other.login(user["username"], user["password"])).status_code == 401
