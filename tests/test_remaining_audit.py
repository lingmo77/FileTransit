"""审计剩余项的传输、删除故障、日志、密码和容量回归。"""
from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models import ActivityLog, FileItem, User
from app.services import activity, cleanup, logs, storage


async def test_unauthorized_upload_does_not_read_body(api):
    received = []
    async def body():
        received.append(True)
        yield b"x" * 2 * 1024**2
    response = await api.client.post("/api/files?filename=x.bin", content=body(),
                                     headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 401
    assert not received


async def test_raw_stream_upload_hash_and_metadata(api, user):
    payload = b"stream\x00" * 1000
    async def body():
        for at in range(0, len(payload), 13):
            yield payload[at:at + 13]
    response = await api.client.post("/api/files", params={"filename": "raw.bin", "expires_hours": 24,
        "is_public": "false"}, content=body(), headers={**api.headers(), "Content-Type": "application/octet-stream"})
    assert response.status_code == 201, response.text
    info = response.json()["file"]
    assert not info["is_public"]
    async with SessionLocal() as db:
        row = await db.get(FileItem, info["id"])
        assert row.sha256 == hashlib.sha256(payload).hexdigest()
    assert (await api.get(info["download_url"])).content == payload


async def test_chunked_body_hard_limit_and_partial_cleanup(api, user, admin):
    original = (await admin.get("/admin/api/settings")).json()["settings"]["quota.max_upload_mb"]
    before = {p for p in settings.storage_dir.rglob("*") if p.is_file()}
    async def body():
        for _ in range(17):
            yield b"x" * 65536
    try:
        await admin.post("/admin/api/settings", json={"quota.max_upload_mb": 1})
        response = await api.client.post("/api/files?filename=chunked.bin", content=body(),
            headers={**api.headers(), "Content-Type": "application/octet-stream"})
        assert response.status_code == 413
        assert {p for p in settings.storage_dir.rglob("*") if p.is_file()} == before
        assert not list(settings.tmp_dir.glob("*.part"))
    finally:
        await admin.post("/admin/api/settings", json={"quota.max_upload_mb": original})


async def test_truncated_multipart_leaves_no_file(api, user):
    before = {p for p in settings.storage_dir.rglob("*") if p.is_file()}
    response = await api.client.post("/api/files", content=(
        b'--test\r\nContent-Disposition: form-data; name="file"; filename="bad.bin"\r\n\r\npartial'),
        headers={**api.headers(), "Content-Type": "multipart/form-data; boundary=test"})
    assert response.status_code == 400
    assert {p for p in settings.storage_dir.rglob("*") if p.is_file()} == before
    assert not list(settings.tmp_dir.glob("*.part"))


async def test_idle_stream_releases_partial_file_and_receive_slot(api, user, monkeypatch):
    monkeypatch.setattr(settings, "upload_idle_timeout_seconds", 0.01)
    before = {p for p in settings.storage_dir.rglob("*") if p.is_file()}
    async def body():
        yield b"partial"
        await asyncio.sleep(0.1)
        yield b"late"
    for _ in range(4):
        response = await api.client.post("/api/files?filename=idle.bin", content=body(),
            headers={**api.headers(), "Content-Type": "application/octet-stream"})
        assert response.status_code == 400
    assert {p for p in settings.storage_dir.rglob("*") if p.is_file()} == before
    assert not list(settings.tmp_dir.glob("*.part"))
    monkeypatch.setattr(settings, "upload_idle_timeout_seconds", 120)
    assert (await api.upload("after-idle.bin", b"123")).status_code == 201


async def test_receive_reservation_remains_until_metadata_commit(api, user, monkeypatch):
    from app.services import quota
    monkeypatch.setattr(settings, "upload_user_concurrency", 1)
    stored, release = asyncio.Event(), asyncio.Event()
    original = quota.consume
    async def pause(*args, **kwargs):
        stored.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(*args, **kwargs)
    monkeypatch.setattr(quota, "consume", pause)
    first = asyncio.create_task(api.upload("commit-paused.bin", b"123"))
    received = []
    async def body():
        received.append(True)
        yield b"456"
    try:
        await asyncio.wait_for(stored.wait(), 5)
        response = await api.client.post("/api/files?filename=second.bin", content=body(),
            headers={**api.headers(), "Content-Type": "application/octet-stream"})
        assert response.status_code == 429
        assert not received
    finally:
        release.set()
        assert (await asyncio.wait_for(first, 10)).status_code == 201
    monkeypatch.setattr(quota, "consume", original)
    assert (await api.upload("after-commit.bin", b"456")).status_code == 201


async def test_stale_update_cannot_modify_reused_file_id(api, user, admin, monkeypatch):
    from app.services import files
    old = (await api.upload("old-id.bin", b"old")).json()["file"]
    read, release = asyncio.Event(), asyncio.Event()
    original = files.update_if_available
    async def pause(db, file_id, values, **kwargs):
        read.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(db, file_id, values, **kwargs)
    monkeypatch.setattr(files, "update_if_available", pause)
    patch = asyncio.create_task(api.patch(f"/api/files/{old['id']}", {"is_public": False}))
    try:
        await asyncio.wait_for(read.wait(), 5)
        assert (await admin.delete(f"/admin/api/files/{old['id']}")).status_code == 200
        new = (await admin.upload("new-id.bin", b"new")).json()["file"]
        assert new["id"] == old["id"]
        assert new["public_id"] != old["public_id"]
    finally:
        release.set()
    assert (await patch).status_code == 409
    assert (await admin.get(f"/api/files/{new['id']}")).json()["file"]["is_public"]


async def test_double_delete_cannot_remove_reused_file_id(api, user, admin, monkeypatch):
    old = (await api.upload("old-double.bin", b"old")).json()["file"]
    async with SessionLocal() as db:
        old_path = (await db.get(FileItem, old["id"])).stored_path
    claimed, release = asyncio.Event(), asyncio.Event()
    original = storage.delete_object
    paused = False
    async def pause(path):
        nonlocal paused
        if path == old_path and not paused:
            paused = True
            claimed.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original(path)
    monkeypatch.setattr(storage, "delete_object", pause)
    first = asyncio.create_task(api.delete(f"/api/files/{old['id']}"))
    try:
        await asyncio.wait_for(claimed.wait(), 5)
        assert (await admin.delete(f"/admin/api/files/{old['id']}")).status_code == 200
        new = (await admin.upload("new-double.bin", b"new")).json()["file"]
        assert new["id"] == old["id"]
    finally:
        release.set()
    assert (await first).status_code == 200
    assert (await admin.get(new["download_url"])).content == b"new"


@pytest.mark.parametrize("admin_route", [False, True])
async def test_failed_manual_delete_retains_index_usage_and_retries(api, user, admin, monkeypatch, admin_route):
    info = (await api.upload("delete-fault.bin", b"123")).json()["file"]
    original = storage.delete_object
    async def fail(*args):
        return storage.DeleteResult.FAILED
    monkeypatch.setattr(storage, "delete_object", fail)
    client, prefix = (admin, "/admin/api/files") if admin_route else (api, "/api/files")
    response = await client.delete(f"{prefix}/{info['id']}")
    assert response.status_code == 503
    async with SessionLocal() as db:
        row = await db.get(FileItem, info["id"])
        assert row is not None and row.is_deleted
        assert storage.resolve(row.stored_path).is_file()
        owner = await db.get(User, user["id"])
        assert (owner.used_files, owner.used_bytes) == (1, 3)
    monkeypatch.setattr(storage, "delete_object", original)
    assert (await client.delete(f"{prefix}/{info['id']}")).status_code == 200
    assert (await api.get("/api/me/quota")).json()["used_files"] == 0


async def test_user_delete_keeps_disabled_user_if_disk_fails(api, user, admin, monkeypatch):
    info = (await api.upload("cascade-fault.bin", b"123")).json()["file"]
    original = storage.delete_object
    async def fail(*args):
        return storage.DeleteResult.FAILED
    monkeypatch.setattr(storage, "delete_object", fail)
    assert (await admin.delete(f"/admin/api/users/{user['id']}")).status_code == 503
    async with SessionLocal() as db:
        owner = await db.get(User, user["id"])
        assert owner is not None and not owner.is_active
        assert await db.get(FileItem, info["id"]) is not None
    monkeypatch.setattr(storage, "delete_object", original)
    assert (await admin.delete(f"/admin/api/users/{user['id']}")).status_code == 200


async def test_audit_failure_does_not_turn_committed_upload_into_error(api, user, monkeypatch, caplog):
    async def failed(**kwargs):
        raise RuntimeError("injected audit failure")
    monkeypatch.setattr(activity, "_write_activity", failed)
    response = await api.upload("audit-fault.bin", b"123")
    assert response.status_code == 201
    assert "审计日志写入失败" in caplog.text
    assert (await api.get(response.json()["file"]["download_url"])).content == b"123"


@pytest.mark.parametrize("named", [False, True])
async def test_slow_download_releases_authenticated_db_before_first_byte(api, user, named):
    from app.main import app
    info = (await api.upload("slow.bin", b"x" * 1024**2)).json()["file"]
    path = info["download_url"] if named else "/d/" + info["public_id"]
    first, release = asyncio.Event(), asyncio.Event()
    request_sent = False
    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
    async def send(message):
        if message["type"] == "http.response.body" and not first.is_set():
            first.set()
            await release.wait()
    cookie = "; ".join(f"{c.name}={c.value}" for c in api.client.cookies.jar)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"cookie", cookie.encode())], "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    before = engine.pool.checkedout()
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first.wait(), 5)
        assert engine.pool.checkedout() == before
    finally:
        release.set()
        await asyncio.wait_for(task, 10)


async def test_export_batches_rows_off_loop_and_removes_temp(admin, monkeypatch):
    async with SessionLocal() as db:
        db.add_all(ActivityLog(action="batch_export", username=f"batch-{i}", status="success") for i in range(1201))
        await db.commit()
    original = logs.export_csv
    batches, threads = [], []
    def recorded(rows, *args, **kwargs):
        if rows:
            batches.append(len(rows))
            threads.append(threading.get_ident())
        return original(rows, *args, **kwargs)
    monkeypatch.setattr(logs, "export_csv", recorded)
    response = await admin.get("/admin/api/logs/export?action=batch_export")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert len(response.text.splitlines()) == 1202
    assert batches == [500, 500, 201]
    assert all(t != threading.get_ident() for t in threads)
    assert not list(settings.tmp_dir.glob("logs-*.csv"))


async def test_purge_does_not_vacuum_by_default(admin, monkeypatch):
    async def forbidden():
        raise AssertionError("unexpected VACUUM")
    monkeypatch.setattr(cleanup, "vacuum_database", forbidden)
    assert (await admin.delete("/admin/api/logs", json={"action": "nonexistent"})).status_code == 200


def test_receive_slots_bound_user_count_and_temp_budget(monkeypatch):
    from app.services.upload_slots import UploadBusy, receive_slot
    monkeypatch.setattr(settings, "upload_user_concurrency", 1)
    monkeypatch.setattr(settings, "upload_temp_max_mb", 2)
    with receive_slot(10001, 1500000):
        with pytest.raises(UploadBusy):
            with receive_slot(10001, 1):
                pass
        with pytest.raises(UploadBusy):
            with receive_slot(10002, 1500000):
                pass
    with receive_slot(10002, 1500000):
        pass


def test_receive_slot_is_shared_with_another_process(monkeypatch):
    import subprocess
    import sys
    from app.services.upload_slots import receive_slot
    monkeypatch.setattr(settings, "upload_user_concurrency", 1)
    code = '''from app.core.config import settings
from app.services.upload_slots import UploadBusy, receive_slot
settings.upload_user_concurrency = 1
try:
    with receive_slot(10003, 1):
        print("unexpected")
except UploadBusy:
    print("busy")
'''
    with receive_slot(10003, 1):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=True)
        assert result.stdout.strip() == "busy"


async def test_reviving_expired_file_cannot_overfill_quota(api, user, admin):
    from datetime import timedelta
    from app.models import utcnow
    from app.services import quota
    await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_files": 1})
    first = (await api.upload("expired.bin", b"123")).json()["file"]
    async with SessionLocal() as db:
        row = await db.get(FileItem, first["id"])
        row.expires_at = utcnow() - timedelta(minutes=1)
        await db.flush()
        await quota.sync_usage(db, user["id"])
        await db.commit()
    assert (await api.upload("new.bin", b"456")).status_code == 201
    assert (await api.patch(f"/api/files/{first['id']}", {"expires_hours": 24})).status_code == 403
    assert (await api.get("/api/me/quota")).json()["used_files"] == 1


async def test_async_password_hash_keeps_event_loop_responsive(monkeypatch):
    import time
    from app.core import security
    event_thread = threading.get_ident()
    def slow(password):
        assert threading.get_ident() != event_thread
        time.sleep(0.1)
        return "hashed"
    monkeypatch.setattr(security, "hash_password", slow)
    task = asyncio.create_task(security.hash_password_async("password123"))
    await asyncio.sleep(0.02)
    assert not task.done()
    assert await task == "hashed"


def test_limiter_expires_unvisited_keys_and_bounds_capacity(monkeypatch):
    from app.core import security
    now = [100.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: now[0])
    limiter = security.RateLimiter(1, 10, max_keys=3)
    for key in ["a", "b", "c"]:
        assert limiter.check(key)
    assert not limiter.check("d")
    assert not limiter.check("a")
    assert len(limiter._hits) == 3
    now[0] += 11
    assert limiter.check("d")
    assert set(limiter._hits) == {"d"}
