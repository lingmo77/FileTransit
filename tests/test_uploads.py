"""Resumption, idempotency, reservations and cleanup against the actual ASGI app."""
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import FileItem, UploadSession, utcnow
from app.services import uploads, storage


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def create(api, data=b"abcdefghij", **extra):
    body = dict(name="resume.bin", size=len(data), sha256=sha(data), expires_hours=24, is_public=True)
    body.update(extra)
    return await api.post("/api/uploads", json=body)


async def chunk(api, uid, data, offset=0, checksum=None):
    return await api.client.patch(f"/api/uploads/{uid}", content=data, headers={
        **api.headers(), "Upload-Offset": str(offset), "X-Chunk-SHA256": checksum or sha(data),
        "Content-Type": "application/octet-stream",
    })


async def test_resume_after_network_failure_and_download_exact_bytes(api, user):
    content = b"original-data-" * 100
    response = await create(api, content)
    assert response.status_code == 201, response.text
    uid = response.json()["upload"]["id"]
    assert (await chunk(api, uid, content[:300])).json()["upload"]["offset"] == 300

    async def broken_connection():
        yield content[300:400]
        raise RuntimeError("simulated broken network")

    with pytest.raises(BaseExceptionGroup):
        await api.client.patch(f"/api/uploads/{uid}", content=broken_connection(), headers={
            **api.headers(), "Upload-Offset": "300", "X-Chunk-SHA256": sha(content[300:]),
        })
    status = (await api.get(f"/api/uploads/{uid}")).json()["upload"]
    assert status["offset"] == 300
    assert uploads.part_path(uid).read_bytes() == content[:300]
    assert (await create(api, content)).json()["upload"]["id"] == uid
    assert (await chunk(api, uid, content[300:], 300)).status_code == 200
    completed = await api.post(f"/api/uploads/{uid}/complete")
    assert completed.status_code == 200, completed.text
    item = completed.json()["file"]
    assert (await api.get(item["download_url"])).content == content
    assert not uploads.part_path(uid).exists()


async def test_lost_chunk_and_completion_responses_do_not_duplicate(api, user):
    data = b"idempotent"
    uid = (await create(api, data)).json()["upload"]["id"]
    assert (await chunk(api, uid, data)).status_code == 200
    assert (await chunk(api, uid, data)).status_code == 409
    assert (await api.get(f"/api/uploads/{uid}")).json()["upload"]["offset"] == len(data)
    first = (await api.post(f"/api/uploads/{uid}/complete")).json()["file"]
    second = (await api.post(f"/api/uploads/{uid}/complete")).json()["file"]
    assert first["id"] == second["id"]
    q = (await api.get("/api/me/quota")).json()
    assert q["used_files"] == 1 and q["used_bytes"] == len(data)
    assert (await api.get(f"/api/uploads/{uid}")).json()["file"]["id"] == first["id"]


async def test_bad_offset_checksum_and_oversize_never_advance(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    assert (await chunk(api, uid, b"abc", 1)).status_code == 409
    assert (await chunk(api, uid, b"abc", checksum="0" * 64)).status_code == 422
    assert (await chunk(api, uid, b"a" * 11)).status_code == 413
    assert (await api.get(f"/api/uploads/{uid}")).json()["upload"]["offset"] == 0
    assert not uploads.part_path(uid).exists()
    assert (await api.post(f"/api/uploads/{uid}/complete")).status_code == 409


async def test_full_file_hash_detects_wrong_content(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    assert (await chunk(api, uid, b"0123456789")).status_code == 200
    assert (await api.post(f"/api/uploads/{uid}/complete")).status_code == 422
    assert not uploads.part_path(uid).exists()
    async with SessionLocal() as db:
        row = await db.get(UploadSession, uid)
        assert row.state == "failed" and row.file_id is None
    assert (await api.get("/api/me/quota")).json()["used_files"] == 0


async def test_uncommitted_tail_is_truncated_on_retry(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    assert (await chunk(api, uid, b"abc")).status_code == 200
    with open(uploads.part_path(uid), "ab") as handle:
        handle.write(b"UNACKNOWLEDGED")
    assert (await chunk(api, uid, b"defghij", 3)).status_code == 200
    assert uploads.part_path(uid).read_bytes() == b"abcdefghij"
    assert (await api.post(f"/api/uploads/{uid}/complete")).status_code == 200


async def test_resume_is_owned_and_requires_csrf(api, user, new_client):
    uid = (await create(api)).json()["upload"]["id"]
    guest = await new_client()
    assert (await guest.get(f"/api/uploads/{uid}")).status_code == 401
    assert (await api.post("/api/uploads", json={"name": "x", "size": 1, "sha256": sha(b"x")}, csrf=False)).status_code == 403
    from .conftest import create_verified_user
    await create_verified_user(guest, "other_resume_")
    assert (await guest.get(f"/api/uploads/{uid}")).status_code == 404
    assert (await chunk(guest, uid, b"abc")).status_code == 404
    assert (await guest.delete(f"/api/uploads/{uid}")).status_code == 404
    assert (await guest.post(f"/api/uploads/{uid}/complete")).status_code == 404
    assert (await api.get("/api/uploads")).json()["items"][0]["id"] == uid


async def test_reservations_block_both_new_and_legacy_uploads(admin, api, user):
    assert (await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_files": 1, "quota_max_bytes": 10})).status_code == 200
    uid = (await create(api)).json()["upload"]["id"]
    assert (await create(api, b"x", name="another.bin")).status_code == 403
    assert (await api.upload("legacy.bin", b"x")).status_code == 403
    assert (await api.delete(f"/api/uploads/{uid}")).status_code == 200
    assert (await create(api, b"x", name="another.bin")).status_code == 201


async def test_concurrent_reservations_cannot_overbook(admin, api, user):
    import asyncio
    await admin.patch(f"/admin/api/users/{user['id']}", {"quota_max_files": 1})
    responses = await asyncio.gather(create(api, b"a", name="a.bin"), create(api, b"b", name="b.bin"))
    assert sorted(r.status_code for r in responses) == [201, 403]


async def test_expiration_frees_parts_but_never_deletes_active_upload(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    await chunk(api, uid, b"abc")
    async with SessionLocal() as db:
        row = await db.get(UploadSession, uid)
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    with uploads.upload_lock(uid):
        assert await uploads.purge_expired_uploads() == 0
        assert uploads.part_path(uid).exists()
        assert (await chunk(api, uid, b"defghij", 3)).status_code == 410
    assert await uploads.purge_expired_uploads() >= 1
    assert not uploads.part_path(uid).exists()
    assert (await api.get(f"/api/uploads/{uid}")).status_code == 200
    assert (await api.post(f"/api/uploads/{uid}/complete")).status_code == 410


async def test_status_queries_do_not_extend_expiration(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    before = (await api.get(f"/api/uploads/{uid}")).json()["upload"]["expires_at"]
    assert (await api.get(f"/api/uploads/{uid}")).json()["upload"]["expires_at"] == before
    await chunk(api, uid, b"abc")
    assert (await api.get(f"/api/uploads/{uid}")).json()["upload"]["expires_at"] > before


async def test_lock_rejects_conflicting_writes_and_cancel(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    with uploads.upload_lock(uid):
        assert (await chunk(api, uid, b"abc")).status_code == 409
        assert (await api.delete(f"/api/uploads/{uid}")).status_code == 409
    assert (await chunk(api, uid, b"abc")).status_code == 200


async def test_server_rename_before_db_commit_is_recoverable(api, user, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    uid = (await create(api)).json()["upload"]["id"]
    await chunk(api, uid, b"abcdefghij")
    original = AsyncSession.commit
    failed = False

    async def fail_once(db):
        nonlocal failed
        if not failed and any(isinstance(row, UploadSession) and row.state == "completed" for row in db.dirty):
            failed = True
            raise RuntimeError("commit interrupted")
        await original(db)

    monkeypatch.setattr(AsyncSession, "commit", fail_once)
    with pytest.raises(RuntimeError, match="commit interrupted"):
        await api.post(f"/api/uploads/{uid}/complete")
    assert not uploads.part_path(uid).exists()
    assert (await api.post(f"/api/uploads/{uid}/complete")).status_code == 200
    assert (await api.get("/api/me/quota")).json()["used_files"] == 1


async def test_size_permissions_and_global_space_limits(api, user, monkeypatch):
    assert (await create(api, expires_hours=0)).status_code == 400
    assert (await create(api, size=21 * 1024**3)).status_code == 413
    monkeypatch.setattr(uploads.settings, "upload_temp_limit_gb", 1)
    assert (await create(api, size=2 * 1024**3)).status_code == 507


async def test_completed_file_is_not_removed_by_upload_cleanup(api, user):
    uid = (await create(api)).json()["upload"]["id"]
    await chunk(api, uid, b"abcdefghij")
    item = (await api.post(f"/api/uploads/{uid}/complete")).json()["file"]
    async with SessionLocal() as db:
        row = await db.get(UploadSession, uid)
        row.expires_at = utcnow() - timedelta(days=1)
        await db.commit()
    await uploads.purge_expired_uploads()
    assert (await api.get(item["download_url"])).content == b"abcdefghij"
    assert (await api.delete(f"/api/uploads/{uid}")).status_code == 409
