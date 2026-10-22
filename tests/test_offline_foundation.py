"""Offline defaults, ownership, bounded metadata and quota concurrency contracts."""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import FileItem, OfflineTask, OfflineTaskFile, User
from app.services import offline, quota, settings_service
from app.services.torrent_metadata import TorrentError, parse_torrent


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


def torrent(*, paths=None, lengths=None):
    paths = paths or [[b"one.txt"], [b"sub", b"two.txt"]]
    lengths = lengths or [4, 5]
    info = {b"name": b"sample", b"piece length": 16,
            b"pieces": b"x" * (20 * ((sum(lengths) + 15) // 16)),
            b"files": [{b"length": size, b"path": path} for size, path in zip(lengths, paths)]}
    return encode({b"announce": b"https://tracker.example/announce", b"info": info})


@pytest.fixture
async def enabled_offline(transport):
    async with SessionLocal() as db:
        before = await settings_service.all_values(db)
        enabled = before.get("offline.enabled", False)
        await settings_service.set_many(db, {"offline.enabled": True})
    yield
    async with SessionLocal() as db:
        await settings_service.set_many(db, {"offline.enabled": enabled})


async def test_offline_http_defaults_private_and_source_is_protected(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks", json={"urls": ["https://93.184.216.34/report.pdf?token=private-token"]})
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["is_public"] is False
    assert task["expires_hours"] == 336
    assert task["imported_at"] is None
    assert "private-token" not in response.text
    async with SessionLocal() as db:
        row = await db.get(OfflineTask, task["id"])
        assert row.source_encrypted and "private-token" not in row.source_encrypted
        assert settings_service.decrypt_secret(row.source_encrypted).endswith("token=private-token")
        owner = await db.get(User, user["id"])
        info = await quota.resolve_quota(db, owner)
        assert owner.used_files == 0
        assert info.reserved_files == 1
        assert info.reserved_bytes > 0


async def test_offline_creation_requires_auth_csrf_and_enablement(api, user):
    response = await api.post("/api/offline/tasks", json={"urls": ["https://93.184.216.34/a"]}, csrf=False)
    assert response.status_code == 403
    async with SessionLocal() as db:
        await settings_service.set_many(db, {"offline.enabled": False})
    response = await api.post("/api/offline/tasks", json={"urls": ["https://93.184.216.34/a"]})
    assert response.status_code == 503
    await api.logout()
    assert (await api.get("/api/offline/tasks")).status_code == 401


async def test_torrent_creation_retries_are_idempotent(api, user, enabled_offline):
    data = {"request_key": "stable-torrent-creation"}
    first = await api.post("/api/offline/tasks/torrent", files={"file": ("sample.torrent", torrent())}, data=data)
    second = await api.post("/api/offline/tasks/torrent", files={"file": ("sample.torrent", torrent())}, data=data)
    assert first.status_code == second.status_code == 201
    assert first.json()["task"]["id"] == second.json()["task"]["id"]


async def test_torrent_file_selection_reserves_exact_selected_amount(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks/torrent", files={"file": ("sample.torrent", torrent(), "application/x-bittorrent")},
                              data={"is_public": "true", "expires_hours": "336"})
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["state"] == "awaiting_selection"
    assert task["is_public"] is True
    assert task["reserved_bytes"] == 0
    assert [f["relative_path"] for f in task["files"]] == ["sample/one.txt", "sample/sub/two.txt"]
    response = await api.post(f'/api/offline/tasks/{task["id"]}/select', json={"indexes": [2]})
    assert response.status_code == 200, response.text
    selected = response.json()["task"]
    assert selected["reserved_files"] == 1
    assert selected["reserved_bytes"] == 5
    assert selected["state"] == "queued"
    assert [f["selected"] for f in selected["files"]] == [False, True]


async def test_other_user_cannot_inspect_or_cancel_task(api, user, enabled_offline, new_client):
    from tests.conftest import create_verified_user
    created = await api.post("/api/offline/tasks", json={"urls": ["magnet:?xt=urn:btih:" + "a" * 40]})
    task_id = created.json()["task"]["id"]
    second = await new_client()
    await create_verified_user(second)
    assert (await second.get(f"/api/offline/tasks/{task_id}")).status_code == 404
    assert (await second.post(f"/api/offline/tasks/{task_id}/action", json={"action": "cancel"})).status_code == 404


async def test_http_reservation_blocks_ordinary_upload_commit(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks", json={"urls": ["https://93.184.216.34/unknown-size"]})
    assert response.status_code == 201
    response = await api.upload("competing.bin", b"ordinary upload")
    assert response.status_code == 403, response.text
    async with SessionLocal() as db:
        owner = await db.get(User, user["id"])
        assert owner.used_files == 0
        assert owner.used_bytes == 0


async def test_batch_quota_excludes_own_reservation_but_counts_other_tasks(api, user, enabled_offline):
    async with SessionLocal() as db:
        owner = await db.get(User, user["id"])
        owner.quota_max_files = 2
        owner.quota_max_bytes = 9
        await db.commit()
        task = await offline.create_torrent_task(db, owner, torrent())
        await offline.select_files(db, owner, task.id, [1, 2])
        await quota.consume_many(db, owner.id, [4, 5], exclude_task_id=task.id)
        counted = await db.get(User, user["id"], populate_existing=True)
        assert (counted.used_files, counted.used_bytes) == (2, 9)
        await db.rollback()
        with pytest.raises(quota.QuotaExceeded):
            await quota.consume_many(db, user["id"], [1])
        await db.rollback()


async def test_idempotent_creation_does_not_duplicate_task_or_reservation(api, user, enabled_offline):
    payload = {"urls": ["https://93.184.216.34/report.pdf"], "request_key": "retry-key"}
    first = await api.post("/api/offline/tasks", json=payload)
    again = await api.post("/api/offline/tasks", json=payload)
    assert first.status_code == again.status_code == 201
    assert first.json()["task"]["id"] == again.json()["task"]["id"]
    payload["urls"] = ["https://93.184.216.34/different.pdf"]
    assert (await api.post("/api/offline/tasks", json=payload)).status_code == 409


async def test_cancel_persists_intent_before_releasing_resources(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks", json={"urls": ["https://93.184.216.34/large.zip"]})
    task_id = response.json()["task"]["id"]
    response = await api.post(f"/api/offline/tasks/{task_id}/action", json={"action": "cancel"})
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["desired_state"] == "cancelled"
    assert task["state"] == "cancelling"
    assert task["reserved_bytes"] > 0


async def test_magnet_normalization_and_invalid_scheme_rejection(api, user, enabled_offline):
    import base64
    value = base64.b32encode(bytes.fromhex("a" * 40)).decode()
    response = await api.post("/api/offline/tasks", json={"urls": ["magnet:?xt=urn:btih:" + value]})
    assert response.status_code == 201, response.text
    async with SessionLocal() as db:
        row = await db.get(OfflineTask, response.json()["task"]["id"])
        assert row.info_hash == "a" * 40
    assert (await api.post("/api/offline/tasks", json={"urls": ["file:///etc/passwd"]})).status_code == 400
    assert (await api.post("/api/offline/tasks", json={"urls": ["http://127.0.0.1/healthz"]})).status_code == 400


def test_torrent_hash_uses_original_info_bytes():
    data = torrent()
    metadata = parse_torrent(data)
    # An intentionally non-canonical dictionary ordering remains hash-stable.
    info = b"d4:name6:sample6:lengthi4e12:piece lengthi16e6:pieces20:" + b"x" * 20 + b"e"
    raw = b"d4:info" + info + b"e"
    assert parse_torrent(raw).info_hash == hashlib.sha1(info).hexdigest()
    assert metadata.files[1].relative_path == "sample/sub/two.txt"


@pytest.mark.parametrize("path", [[b"..", b"secret"], [b"/etc/passwd"], [b"a\\b"], [b"C:", b"file"]])
def test_torrent_rejects_path_escape(path):
    with pytest.raises(TorrentError):
        parse_torrent(torrent(paths=[path], lengths=[4]))


def test_torrent_enforces_count_depth_and_piece_constraints():
    with pytest.raises(TorrentError, match="数量"):
        parse_torrent(torrent(), max_files=1)
    with pytest.raises(TorrentError):
        parse_torrent(b"l" * 40 + b"e" * 40)
    broken = encode({b"info": {b"name": b"bad", b"length": 4, b"piece length": 0, b"pieces": b"x" * 20}})
    with pytest.raises(TorrentError, match="分块"):
        parse_torrent(broken)


async def test_rejected_selection_does_not_leave_reservation(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks/torrent", files={"file": ("sample.torrent", torrent(), "application/x-bittorrent")})
    task_id = response.json()["task"]["id"]
    async with SessionLocal() as db:
        owner = await db.get(User, user["id"])
        owner.quota_max_bytes = 4
        await db.commit()
    response = await api.post(f"/api/offline/tasks/{task_id}/select", json={"indexes": [1, 2]})
    assert response.status_code == 403
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        assert task.reserved_bytes == task.reserved_files == 0
        assert task.state == "awaiting_selection"


async def test_awaiting_selection_cannot_bypass_confirmation_through_pause(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks/torrent", files={"file": ("sample.torrent", torrent(), "application/x-bittorrent")})
    task_id = response.json()["task"]["id"]
    assert (await api.post(f"/api/offline/tasks/{task_id}/action", json={"action": "pause"})).status_code == 409
    assert (await api.post(f"/api/offline/tasks/{task_id}/action", json={"action": "resume"})).status_code == 409
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        assert task.state == "awaiting_selection"
        assert task.reserved_files == 0


async def test_batch_partial_failure_uses_fresh_owner_after_rollback(api, user, enabled_offline):
    response = await api.post("/api/offline/tasks", json={"urls": ["file:///invalid", "magnet:?xt=urn:btih:" + "b" * 40]})
    assert response.status_code == 201, response.text
    result = response.json()
    assert len(result["tasks"]) == len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 0
