"""上传、下载（含 Range 多线程）、配额、可见性与删除。"""

from __future__ import annotations

from .conftest import count_stored_blobs, create_verified_user, stored_blob_bytes

PAYLOAD = bytes(range(256)) * 40  # 10240 字节，内容可逐字节校验


async def test_guest_can_browse_and_download_without_login(api, user, new_client):
    """需求 1：游客无需登录即可看到首页文件并直接下载。"""
    response = await api.upload("guest-demo.bin", PAYLOAD)
    assert response.status_code == 201, response.text
    info = response.json()["file"]

    guest = await new_client()  # 全新会话，未登录
    index = await guest.get("/")
    assert index.status_code == 200
    assert "guest-demo.bin" in index.text

    assert (await guest.get(f"/f/{info['public_id']}")).status_code == 200

    download = await guest.get(f"/d/{info['public_id']}")
    assert download.status_code == 200
    assert download.content == PAYLOAD


async def test_upload_requires_login(api):
    response = await api.client.post(
        "/api/files",
        files={"file": ("x.bin", b"abc", "application/octet-stream")},
        data={"expires_hours": "24", "is_public": "true"},
        headers=api.headers(),
    )
    assert response.status_code == 401


async def test_expiry_is_clamped_to_site_maximum(api, user):
    response = await api.upload("clamp.bin", b"x" * 10, expires_hours=10000)
    assert response.status_code == 201

    from app.core.database import SessionLocal
    from app.services import files as files_service
    from app.services import settings_service

    async with SessionLocal() as db:
        max_days = await settings_service.get_int(db, "quota.max_expire_days", 30)
        item = await files_service.get_by_public_id(db, response.json()["file"]["public_id"])
        lifetime_hours = (item.expires_at - item.created_at).total_seconds() / 3600

    assert lifetime_hours <= max_days * 24 + 1


async def test_range_requests_enable_multithreaded_download(api, user):
    """需求 8：IDM / 迅雷 这类多线程下载依赖 HTTP Range。"""
    response = await api.upload("range.bin", PAYLOAD)
    info = response.json()["file"]
    url = f"/d/{info['public_id']}"
    size = len(PAYLOAD)

    head = await api.client.request("HEAD", url)
    assert head.status_code == 200
    assert head.headers["accept-ranges"] == "bytes"
    assert int(head.headers["content-length"]) == size

    full = await api.get(url)
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.content == PAYLOAD

    first = await api.get(url, headers={"Range": "bytes=0-99"})
    assert first.status_code == 206
    assert first.headers["content-range"] == f"bytes 0-99/{size}"
    assert first.content == PAYLOAD[:100]

    middle = await api.get(url, headers={"Range": "bytes=500-999"})
    assert middle.status_code == 206
    assert middle.content == PAYLOAD[500:1000]

    tail = await api.get(url, headers={"Range": "bytes=-100"})
    assert tail.status_code == 206
    assert tail.content == PAYLOAD[-100:]

    assert (await api.get(url, headers={"Range": f"bytes={size}-"})).status_code == 416

    friendly = await api.get(
        f"/download/{info['public_id']}/range.bin", headers={"Range": "bytes=10-19"}
    )
    assert friendly.status_code == 206
    assert friendly.content == PAYLOAD[10:20]


def _assert_valid_etag(etag: str) -> str:
    """ETag 必须是 RFC 7232 的合法 opaque-tag。

    ``etagc = %x21 / %x23-7E``——双引号之内不允许空格（``%x20``）、双引号本身
    以及不可见字符。
    """
    assert etag.startswith('"') and etag.endswith('"'), etag
    inner = etag[1:-1]
    assert inner, "ETag 不能是空串"
    for ch in inner:
        assert ch != '"', f"ETag 里不能有裸双引号: {etag!r}"
        assert 0x21 <= ord(ch) <= 0x7E, f"ETag 含非法字符 {ch!r}: {etag!r}"
    return inner


async def test_etag_is_valid_and_if_range_round_trips(api, user):
    """回归：ETag 里曾经直接拼了 ``expires_at``，datetime 的 str() 带空格。

    那个空格违反 RFC 7232，下载器回传的 ``If-Range`` 就对不上；Starlette 的
    ``FileResponse`` 只要 ``If-Range`` 匹配失败就返回 ``200`` 整包而不是 ``206``，
    分片连接收到整包只能断开——表现为「多线程下载只连得上一条」。
    """
    response = await api.upload("etag.bin", PAYLOAD)
    info = response.json()["file"]
    url = f"/d/{info['public_id']}"

    head = await api.client.request("HEAD", url)
    assert head.status_code == 200
    etag = head.headers["etag"]
    _assert_valid_etag(etag)

    # 内容不变时 ETag 必须稳定（同一个 public_id 内容永不改变）
    assert (await api.client.request("HEAD", url)).headers["etag"] == etag

    # 原样回传必须仍然是 206：断点续传/多线程分片就靠这个
    ranged = await api.get(url, headers={"Range": "bytes=100-199", "If-Range": etag})
    assert ranged.status_code == 206, (ranged.status_code, ranged.headers)
    assert ranged.content == PAYLOAD[100:200]

    # 对不上的 If-Range 会被 Starlette 降级成 200 整包。
    # 固化这个行为，好让以后有人再往 ETag 里塞会变的值时立刻挂测试。
    stale = await api.get(url, headers={"Range": "bytes=100-199", "If-Range": '"stale"'})
    assert stale.status_code == 200
    assert stale.content == PAYLOAD


async def test_forever_file_has_a_valid_etag(admin, api):
    """长期有效的文件（``expires_at`` 为 NULL）ETag 同样要合法、且不含多余字段。"""
    response = await admin.upload("forever-etag.bin", PAYLOAD, expires_hours=0)
    assert response.status_code == 201, response.text
    info = response.json()["file"]
    assert info["expires_at"] is None

    head = await api.client.request("HEAD", f"/d/{info['public_id']}")
    assert head.status_code == 200
    etag = head.headers["etag"]
    _assert_valid_etag(etag)
    # 有效期不再是 ETag 的组成部分：内容没变，ETag 就不该变
    assert etag == f'"{info["public_id"]}-{len(PAYLOAD)}"'


async def test_html_upload_is_forced_to_download(api, user):
    """避免上传的 HTML/SVG 在站点域下被渲染执行（存储型 XSS）。"""
    response = await api.upload("evil.html", b"<script>alert(1)</script>")
    assert response.status_code == 201
    info = response.json()["file"]

    download = await api.get(f"/d/{info['public_id']}")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in download.headers["content-disposition"]


async def _download_count(api, file_id: int) -> int:
    response = await api.get(f"/api/files/{file_id}")
    assert response.status_code == 200, response.text
    return response.json()["file"]["download_count"]


async def test_download_count_ignores_parallel_chunks(api, user):
    """需求 8：IDM/迅雷 会把一次下载切成几十个分片，计数不能跟着暴涨。

    约定：只有「非 0 起始」的分片和非 GET（HEAD 探测）不计；从 0 开始的分片
    或整包下载各计一次。
    """
    response = await api.upload("counted.bin", PAYLOAD)
    info = response.json()["file"]
    url = f"/d/{info['public_id']}"

    # 下载器先 HEAD 探测大小，再并发拉取后续分片
    await api.client.request("HEAD", url)
    for start in (512, 1024, 2048, 4096):
        chunk = await api.get(url, headers={"Range": f"bytes={start}-{start + 511}"})
        assert chunk.status_code == 206
    assert await _download_count(api, info["id"]) == 0

    # 首个从 0 开始的分片才算一次完整下载
    first = await api.get(url, headers={"Range": "bytes=0-511"})
    assert first.status_code == 206
    assert await _download_count(api, info["id"]) == 1

    for start in (512, 1024):
        await api.get(url, headers={"Range": f"bytes={start}-{start + 511}"})
    assert await _download_count(api, info["id"]) == 1

    # 不带 Range 的整包下载算第二次
    assert (await api.get(url)).status_code == 200
    assert await _download_count(api, info["id"]) == 2


async def test_quota_limits_concurrent_file_count(admin, api, user):
    """需求 3：默认同时存在 10 个未过期文件。这里把上限压到 1 个来验证。"""
    response = await admin.patch(
        f"/admin/api/users/{user['id']}", {"quota_max_files": 1}
    )
    assert response.status_code == 200

    first = await api.upload("quota-1.bin", b"a" * 100)
    assert first.status_code == 201

    second = await api.upload("quota-2.bin", b"b" * 100)
    assert second.status_code == 403
    assert "上限" in second.json()["error"]

    # 删掉后就释放了名额
    assert (await api.delete(f"/api/files/{first.json()['file']['id']}")).status_code == 200
    assert (await api.upload("quota-3.bin", b"c" * 100)).status_code == 201

    await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_files": None})


async def test_quota_limits_total_bytes_and_leaves_nothing_behind(admin, api, user):
    from app.core.config import settings

    before = {p for p in settings.storage_dir.rglob("*") if p.is_file()}

    response = await admin.patch(
        f"/admin/api/users/{user['id']}", {"quota_max_bytes": 1024}
    )
    assert response.status_code == 200

    response = await api.upload("too-big.bin", b"x" * 4096)
    assert response.status_code in (403, 413)

    # 配额拒绝后不能留下孤儿文件或数据库记录
    after = {p for p in settings.storage_dir.rglob("*") if p.is_file()}
    assert after == before

    listing = await admin.get("/admin/api/files", params={"user_id": user["id"]})
    assert listing.json()["total"] == 0

    await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_bytes": None})


async def test_delete_own_file(api, user):
    response = await api.upload("to-delete.bin", b"delete me")
    info = response.json()["file"]

    assert (await api.delete(f"/api/files/{info['id']}")).status_code == 200
    assert (await api.get(f"/d/{info['public_id']}")).status_code == 404
    assert (await api.get(f"/f/{info['public_id']}")).status_code == 404


async def test_private_file_hidden_from_others(api, user, new_client):
    response = await api.upload("private.bin", b"secret", is_public=False)
    info = response.json()["file"]

    assert "private.bin" not in (await api.get("/")).text

    guest = await new_client()
    assert (await guest.get(f"/f/{info['public_id']}")).status_code == 404
    assert (await guest.get(f"/d/{info['public_id']}")).status_code == 404

    # 所有者自己仍然可以访问
    assert (await api.get(f"/f/{info['public_id']}")).status_code == 200


async def test_other_users_cannot_delete_my_file(api, user, new_client):
    response = await api.upload("mine.bin", b"mine")
    info = response.json()["file"]

    other = await new_client()
    await create_verified_user(other, prefix="other_")

    assert (await other.delete(f"/api/files/{info['id']}")).status_code == 403
    assert (await api.get(f"/d/{info['public_id']}")).status_code == 200


# ---------------------------------------------------------------- 长期有效


async def test_admin_can_set_forever_expiry(admin, api, new_client):
    """管理员上传时选「长期有效」→ ``expires_at`` 存 NULL，且全站可见、可下载。"""
    response = await admin.upload("forever.bin", PAYLOAD, expires_hours=0)
    assert response.status_code == 201, response.text
    info = response.json()["file"]

    assert info["expires_at"] is None
    assert info["forever"] is True

    # 首页文件墙要能捞到（NULL 参与比较恒为假，漏写 IS NULL 就会凭空消失）
    guest = await new_client()
    assert "forever.bin" in (await guest.get("/")).text

    # 下载要能走通：ETag 里用到 expires_at，对 None 会崩
    download = await guest.get(f"/d/{info['public_id']}")
    assert download.status_code == 200
    assert download.content == PAYLOAD
    assert 'ETag' in download.headers

    detail = await guest.get(f"/f/{info['public_id']}")
    assert detail.status_code == 200
    assert "长期有效" in detail.text

    # 详情接口也要能正常序列化（None 不能崩）
    detail_json = await admin.get(f"/api/files/{info['id']}")
    assert detail_json.status_code == 200
    assert detail_json.json()["file"]["expires_at"] is None


async def test_normal_user_cannot_set_forever_expiry(api, user):
    """普通用户即便伪造 ``expires_hours=0`` 也必须被后端挡下。"""
    before_count = count_stored_blobs()
    before_bytes = stored_blob_bytes()

    response = await api.upload("nope.bin", b"x" * 32, expires_hours=0)
    assert response.status_code == 400, response.text

    # 拒绝必须发生在写盘之前，否则磁盘上会留下没有数据库记录的孤儿文件
    assert count_stored_blobs() == before_count, "被拒的上传仍在存储目录留下文件"
    assert stored_blob_bytes() == before_bytes, "存储目录体积发生了变化"

    # 修改路径同样要拦
    uploaded = await api.upload("normal.bin", b"x" * 32)
    file_id = uploaded.json()["file"]["id"]
    patched = await api.patch(f"/api/files/{file_id}", {"expires_hours": 0})
    assert patched.status_code == 400, patched.text


async def test_admin_can_make_existing_file_forever(admin, api, user):
    """后台可以把已经存在的文件改成长期有效。"""
    uploaded = await api.upload("promote.bin", b"y" * 64)
    info = uploaded.json()["file"]
    assert info["expires_at"] is not None

    response = await admin.patch(
        f"/admin/api/files/{info['id']}", {"expires_hours": 0}
    )
    assert response.status_code == 200, response.text
    assert response.json()["file"]["expires_at"] is None

    assert (await admin.get(f"/d/{info['public_id']}")).status_code == 200


async def _grant_forever(admin, user_id: int, allowed: bool = True):
    response = await admin.patch(f"/admin/api/users/{user_id}", {"can_permanent": allowed})
    assert response.status_code == 200, response.text
    return response


async def test_granted_user_can_save_forever(admin, api, user, new_client):
    """需求：管理员指定某个用户后，这个普通用户也能永久保存文件。

    权限是**按用户发放**的，不再是「只有管理员」——所以这里走的是普通用户
    自己的会话 ``api``，全程没碰后台接口。
    """
    # 授权之前必须被拦（对照组，证明下面放行确实是授权带来的）
    assert (await api.upload("before-grant.bin", b"a" * 16, expires_hours=0)).status_code == 400

    await _grant_forever(admin, user["id"])

    response = await api.upload("granted.bin", PAYLOAD, expires_hours=0)
    assert response.status_code == 201, response.text
    info = response.json()["file"]
    assert info["expires_at"] is None
    assert info["forever"] is True

    # 长期有效的文件必须能出现在首页并被下载（NULL 参与比较恒为假，
    # active_clause 漏写 IS NULL 就会两边都捞不到）
    guest = await new_client()
    assert "granted.bin" in (await guest.get("/")).text
    download = await guest.get(f"/d/{info['public_id']}")
    assert download.status_code == 200
    assert download.content == PAYLOAD

    # 修改路径同样放行：把已有文件改成长期有效
    normal = await api.upload("promote-granted.bin", b"b" * 32)
    patched = await api.patch(
        f"/api/files/{normal.json()['file']['id']}", {"expires_hours": 0}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["file"]["expires_at"] is None


async def test_revoked_user_keeps_existing_forever_files(admin, api, user):
    """收回权限后不能再设长期有效，但**已有的**长期有效文件不追溯删除。"""
    await _grant_forever(admin, user["id"])
    kept = (await api.upload("already-forever.bin", b"c" * 16, expires_hours=0)).json()["file"]
    assert kept["expires_at"] is None

    await _grant_forever(admin, user["id"], allowed=False)

    # 新上传被拦
    assert (await api.upload("after-revoke.bin", b"d" * 16, expires_hours=0)).status_code == 400
    # 老文件原样保留，且仍在首页
    assert (await api.get(f"/api/files/{kept['id']}")).json()["file"]["expires_at"] is None
    assert "already-forever.bin" in (await api.get("/")).text


async def test_dashboard_offers_forever_only_when_granted(admin, api, user):
    """上传页的「长期有效」选项跟着授权走，授权前后同一个人看到的不一样。"""
    before = await api.get("/dashboard")
    assert before.status_code == 200
    assert 'data-can-forever="false"' in before.text
    assert "长期有效（永久保存）" not in before.text

    await _grant_forever(admin, user["id"])

    after = await api.get("/dashboard")
    assert after.status_code == 200
    assert 'data-can-forever="true"' in after.text
    assert "长期有效（永久保存）" in after.text


async def test_max_upload_mb_setting_takes_effect_without_restart(admin, api, user):
    """后台改「单文件上限」立刻生效——上传时实时读库，不是启动时读环境变量。

    回归点：``FT_MAX_UPLOAD_MB`` 只是首次建库的播种默认值，改它不影响已建好的库；
    真正说了算的是后台设置 ``quota.max_upload_mb``。
    """
    original = (await admin.get("/admin/api/settings")).json()["settings"]
    original_mb = original["quota.max_upload_mb"]

    try:
        # 收紧到 1MB：2MB 的文件必须被拒
        response = await admin.post("/admin/api/settings", json={"quota.max_upload_mb": 1})
        assert response.status_code == 200, response.text
        assert response.json()["settings"]["quota.max_upload_mb"] == 1

        payload = b"z" * (2 * 1024 * 1024)
        rejected = await api.upload("over-limit.bin", payload)
        assert rejected.status_code == 413, rejected.text
        assert "1 MB" in rejected.text

        # 放宽到 20MB：同一个文件立刻就能传了（无需重启进程）
        assert (
            await admin.post("/admin/api/settings", json={"quota.max_upload_mb": 20})
        ).status_code == 200

        accepted = await api.upload("under-limit.bin", payload)
        assert accepted.status_code == 201, accepted.text

        # 前台「单文件上限」展示的也是这个值
        assert "20 MB" in (await api.get("/dashboard")).text
    finally:
        await admin.post(
            "/admin/api/settings", json={"quota.max_upload_mb": original_mb}
        )
