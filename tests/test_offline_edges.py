"""Reconcile cancellation, concurrency and BT verification at engine boundaries."""
from __future__ import annotations

from sqlalchemy import delete, func, select, update

from app.core.database import SessionLocal
from app.models import FileItem, OfflineTask, OfflineTransfer
from app.services import offline_storage as disk, offline_worker as worker
from app.services.aria2_rpc import Aria2Error
from tests.test_offline_worker import complete_status, engine, task_for, values


async def test_never_submitted_cancel_releases_budget_while_engine_is_offline(api, user, engine, monkeypatch):
    task_id, gid, _ = await task_for(user, state="queued")
    async with SessionLocal() as db:
        await db.execute(delete(OfflineTransfer).where(OfflineTransfer.task_id == task_id))
        task = await db.get(OfflineTask, task_id)
        task.desired_state = "cancelled"
        task.state = "cancelling"
        task.cleanup_pending = True
        await db.commit()

    async def unavailable(*args, **kwargs):
        raise Aria2Error("下载引擎暂时不可用")

    monkeypatch.setattr(engine, "call", unavailable)
    monkeypatch.setattr(engine, "status", unavailable)
    await worker._handle(task_id, await values(), False)

    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        assert task.state == "cancelled"
        assert task.reserved_files == task.reserved_bytes == 0
        assert task.cleanup_pending is False
    assert not disk.task_dir(task_id).exists()


async def test_existing_paused_transfers_obey_user_concurrency_when_resuming(api, user, engine):
    first_id, first_gid, first_data = await task_for(user, state="queued")
    second_id, second_gid, second_data = await task_for(user, state="queued")
    async with SessionLocal() as db:
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.task_id.in_([first_id, second_id])).values(state="paused"))
        await db.commit()
    engine.entries[first_gid] = complete_status(first_id, first_gid, first_data, state="paused")
    engine.entries[second_gid] = complete_status(second_id, second_gid, second_data, state="paused")
    config = await values(**{"offline.user_concurrency": 1})

    await worker._handle(first_id, config, False)
    await worker._handle(second_id, config, False)

    resumed = [args[0] for method, args in engine.calls if method == "unpause"]
    assert len(resumed) == 1
    assert sum(entry["status"] == "active" for entry in engine.entries.values()) == 1


async def test_bt_integrity_check_does_not_import_fully_counted_unverified_payload(api, user, engine):
    task_id, gid, data = await task_for(user, kind="torrent")
    status = complete_status(task_id, gid, data, kind="torrent", state="active")
    status.update({
        "totalLength": str(sum(len(content) for _, content in data)),
        "completedLength": str(sum(len(content) for _, content in data)),
        "verifiedLength": "1",
        "verifyIntegrityPending": False,
        "seeder": "false",
    })
    engine.entries[gid] = status

    await worker._handle(task_id, await values(), False)

    assert task_id not in worker._imports
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        assert task.imported_at is None
        assert await db.scalar(select(func.count()).select_from(FileItem).where(FileItem.user_id == user["id"])) == 0


async def test_verified_payload_saves_without_engine_after_restart(api, user, engine, monkeypatch):
    task_id, gid, _ = await task_for(user, state="saving")
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        task.payload_ready = True
        await db.commit()

    async def unavailable(*args, **kwargs):
        raise Aria2Error("下载引擎暂时不可用")

    monkeypatch.setattr(engine, "call", unavailable)
    monkeypatch.setattr(engine, "status", unavailable)
    await worker._handle(task_id, await values(), False)
    assert task_id in worker._imports
    await worker._imports[task_id][0]
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, task_id)
        assert task.imported_at is not None and task.reserved_files == 0
