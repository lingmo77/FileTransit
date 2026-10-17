"""1.1.2 审计前三项：确定性竞态、缓存权限与配额事务回归。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import FileItem, User, utcnow
from app.services import cleanup, files, quota, storage


async def expired_upload(api):
    response = await api.upload("race.bin", b"keep after renewal")
    assert response.status_code == 201, response.text
    info = response.json()["file"]
    async with SessionLocal() as db:
        item = await db.get(FileItem, info["id"])
        item.expires_at = utcnow() - timedelta(minutes=1)
        path = storage.resolve(item.stored_path)
        await db.commit()
    return info, path


@pytest.mark.parametrize("admin_route,hours", [(False, 24), (True, 24), (True, 0)])
async def test_renewal_wins_before_cleanup_claim(api, user, admin, monkeypatch, admin_route, hours):
    info, path = await expired_upload(api)
    selected, resume = asyncio.Event(), asyncio.Event()
    original = cleanup.claim_expired_file

    async def paused(db, file_id, cutoff, **kwargs):
        if file_id == info["id"]:
            selected.set()
            await asyncio.wait_for(resume.wait(), 10)
        return await original(db, file_id, cutoff, **kwargs)

    monkeypatch.setattr(cleanup, "claim_expired_file", paused)
    task = asyncio.create_task(cleanup.purge_expired_files(batch_size=1))
    try:
        await asyncio.wait_for(selected.wait(), 5)
        client, prefix = (admin, "/admin/api/files") if admin_route else (api, "/api/files")
        response = await client.patch(f"{prefix}/{info['id']}", {"expires_hours": hours})
        assert response.status_code == 200, response.text
    finally:
        resume.set()
        await asyncio.wait_for(task, 10)

    assert path.is_file()
    assert (await api.get(info["download_url"])).status_code == 200


@pytest.mark.parametrize("admin_route", [False, True])
async def test_cleanup_claim_blocks_stale_renewal(api, user, admin, monkeypatch, admin_route):
    """修改接口已读到记录，但在条件写入前被清理认领，必须失败。"""
    info, path = await expired_upload(api)
    read, allow_update = asyncio.Event(), asyncio.Event()
    claimed, allow_delete = asyncio.Event(), asyncio.Event()
    original_update, original_delete = files.update_if_available, storage.delete_object

    async def paused_update(db, file_id, values, **kwargs):
        if file_id == info["id"]:
            read.set()
            await asyncio.wait_for(allow_update.wait(), 10)
        return await original_update(db, file_id, values, **kwargs)

    async def paused_delete(rel_path):
        if storage.resolve(rel_path) == path:
            claimed.set()
            await asyncio.wait_for(allow_delete.wait(), 10)
        return await original_delete(rel_path)

    monkeypatch.setattr(files, "update_if_available", paused_update)
    monkeypatch.setattr(storage, "delete_object", paused_delete)
    client, prefix = (admin, "/admin/api/files") if admin_route else (api, "/api/files")
    renewal = asyncio.create_task(client.patch(f"{prefix}/{info['id']}", {"expires_hours": 24}))
    purge = None
    try:
        await asyncio.wait_for(read.wait(), 5)
        purge = asyncio.create_task(cleanup.purge_expired_files())
        await asyncio.wait_for(claimed.wait(), 5)
        allow_update.set()
        response = await asyncio.wait_for(renewal, 5)
        assert response.status_code == 409, response.text
        assert path.exists()  # 磁盘尚未删除时也不能错误报告续期成功
    finally:
        allow_update.set()
        allow_delete.set()
        await asyncio.wait_for(renewal, 10)
        if purge:
            await asyncio.wait_for(purge, 10)
    assert not path.exists()
    async with SessionLocal() as db:
        assert await db.get(FileItem, info["id"]) is None


async def test_cleanup_failed_claim_is_retryable(api, user, monkeypatch):
    info, path = await expired_upload(api)
    original = storage.delete_object

    async def fail(rel_path):
        if storage.resolve(rel_path) == path:
            return False
        return await original(rel_path)

    monkeypatch.setattr(storage, "delete_object", fail)
    await cleanup.purge_expired_files(batch_size=1)
    assert path.is_file()
    async with SessionLocal() as db:
        item = await db.get(FileItem, info["id"])
        assert item is not None and item.is_deleted
    assert (await api.patch(f"/api/files/{info['id']}", {"expires_hours": 24})).status_code == 404

    monkeypatch.setattr(storage, "delete_object", original)
    await cleanup.purge_expired_files(batch_size=1)
    assert not path.exists()
    async with SessionLocal() as db:
        assert await db.get(FileItem, info["id"]) is None


async def test_cleanup_resumes_claim_after_blob_already_removed(api, user):
    info, path = await expired_upload(api)
    async with SessionLocal() as db:
        assert await cleanup.claim_expired_file(db, info["id"], utcnow())
    await storage.delete_object(path.relative_to(storage.settings.storage_dir.resolve()).as_posix())
    await cleanup.purge_expired_files()
    async with SessionLocal() as db:
        assert await db.get(FileItem, info["id"]) is None


@pytest.mark.parametrize("is_public", [False, True])
async def test_downloads_never_allow_shared_or_fresh_caches(api, user, new_client, is_public):
    payload = b"download cache policy"
    info = (await api.upload("cache.bin", payload, is_public=is_public)).json()["file"]
    client = await new_client() if is_public else api
    for url in (f"/d/{info['public_id']}", info["download_url"]):
        for method, headers, expected in (
            ("GET", {}, 200), ("HEAD", {}, 200), ("GET", {"Range": "bytes=0-3"}, 206),
        ):
            response = await client.client.request(method, url, headers=headers)
            assert response.status_code == expected
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["accept-ranges"] == "bytes"
            assert response.headers["etag"] == f'"{info["public_id"]}-{len(payload)}"'


async def test_old_etag_does_not_bypass_revoked_access(api, user, new_client, admin):
    info = (await api.upload("revoke.bin", b"secret")).json()["file"]
    guest = await new_client()
    url = info["download_url"]
    etag = (await guest.get(url)).headers["etag"]
    headers = {"If-None-Match": etag, "If-Range": etag, "Range": "bytes=0-1"}
    assert (await api.patch(f"/api/files/{info['id']}", {"is_public": False})).status_code == 200
    assert (await guest.get(url, headers=headers)).status_code == 404
    assert (await api.patch(f"/api/files/{info['id']}", {"is_public": True})).status_code == 200
    cfg = (await admin.get("/admin/api/settings")).json()["settings"]
    try:
        await admin.post("/admin/api/settings", json={"site.allow_guest_download": False})
        assert (await guest.get(url, headers=headers)).status_code == 401
    finally:
        await admin.post("/admin/api/settings", json={"site.allow_guest_download": cfg["site.allow_guest_download"]})
    async with SessionLocal() as db:
        item = await db.get(FileItem, info["id"])
        item.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await guest.get(url, headers=headers)).status_code == 410


@pytest.mark.parametrize("max_files,max_bytes,accepted", [(1, 100, 1), (10, 5, 1), (-1, -1, 2)])
async def test_parallel_uploads_serialize_quota(api, user, admin, monkeypatch, max_files, max_bytes, accepted):
    response = await admin.patch(f"/admin/api/users/{user['id']}", {
        "quota_max_files": max_files, "quota_max_bytes": max_bytes,
    })
    assert response.status_code == 200
    original = storage.store_upload
    both_stored = asyncio.Event()
    stored_paths = []

    async def paused(*args, **kwargs):
        stored = await original(*args, **kwargs)
        stored_paths.append(storage.resolve(stored.rel_path))
        if len(stored_paths) == 2:
            both_stored.set()
        await asyncio.wait_for(both_stored.wait(), 5)
        return stored

    monkeypatch.setattr(storage, "store_upload", paused)
    responses = await asyncio.gather(api.upload("one.bin", b"123"), api.upload("two.bin", b"456"))
    assert sorted(r.status_code for r in responses) == [201] * accepted + [403] * (2 - accepted)
    usage = (await api.get("/api/me/quota")).json()
    assert (usage["used_files"], usage["used_bytes"]) == (accepted, accepted * 3)
    assert sum(path.exists() for path in stored_paths) == accepted
    async with SessionLocal() as db:
        rows = (await db.scalars(select(FileItem).where(FileItem.user_id == user["id"]))).all()
        assert len(rows) == accepted


async def test_upload_failure_rolls_back_quota_and_disk(api, user, admin, monkeypatch):
    await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_files": 1})
    original_consume, original_store = quota.consume, storage.store_upload
    stored_paths = []

    async def record(*args, **kwargs):
        stored = await original_store(*args, **kwargs)
        stored_paths.append(storage.resolve(stored.rel_path))
        return stored

    async def fail_after_consume(*args, **kwargs):
        await original_consume(*args, **kwargs)
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(storage, "store_upload", record)
    monkeypatch.setattr(quota, "consume", fail_after_consume)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        await api.upload("failed.bin", b"123")
    assert not any(path.exists() for path in stored_paths)
    async with SessionLocal() as db:
        owner = await db.get(User, user["id"])
        assert (owner.used_files, owner.used_bytes) == (0, 0)
        assert not (await db.scalars(select(FileItem).where(FileItem.user_id == user["id"]))).all()
    monkeypatch.setattr(quota, "consume", original_consume)
    assert (await api.upload("retry.bin", b"123")).status_code == 201
