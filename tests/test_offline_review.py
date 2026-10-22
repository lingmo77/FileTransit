"""Regression cases found by the second offline-download audit."""
from __future__ import annotations

import threading
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.core.config import settings
from app.models import OfflineTask, OfflineTransfer
from app.services import aria2_rpc, offline, offline_worker as worker
from app.services.aria2_rpc import Aria2Error, Aria2ResponseTooLarge, Aria2RPC
from app.services.torrent_metadata import TorrentError, parse_torrent
from tests.test_offline_foundation import torrent
from tests.test_offline_worker import complete_status, engine, task_for, values


async def test_pausing_releases_the_user_download_slot(api, user, engine):
    first, first_gid, first_data = await task_for(user)
    second, second_gid, second_data = await task_for(user, state="queued")
    await worker._set_task(first, desired_state="paused")
    async with SessionLocal() as db:
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == first_gid).values(state="active"))
        await db.commit()
    engine.entries[first_gid] = complete_status(first, first_gid, first_data, state="active")
    engine.entries[second_gid] = complete_status(second, second_gid, second_data, state="paused")
    config = await values(**{"offline.user_concurrency": 1})

    await worker._handle(first, config, False)
    await worker._handle(second, config, False)

    assert engine.entries[first_gid]["status"] == "paused"
    assert engine.entries[second_gid]["status"] == "active"


async def test_saved_seeding_task_does_not_consume_download_slot(api, user, engine):
    first, first_gid, first_data = await task_for(user, kind="torrent")
    engine.entries[first_gid] = complete_status(first, first_gid, first_data, kind="torrent", state="active")
    await worker._import(first, threading.Event())
    config = await values(**{"offline.seed_enabled": True, "offline.user_concurrency": 1})
    await worker._handle(first, config, False)
    second, second_gid, second_data = await task_for(user, state="queued")
    engine.entries[second_gid] = complete_status(second, second_gid, second_data, state="paused")

    await worker._handle(second, config, False)

    assert engine.entries[second_gid]["status"] == "active"


async def test_error_task_retries_stopping_a_leftover_transfer(api, user, engine):
    identifier, gid, data = await task_for(user, state="error")
    engine.entries[gid] = complete_status(identifier, gid, data, state="active")
    await worker._handle(identifier, await values(), False)
    assert gid not in engine.entries
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask, identifier)).state == "error"


async def test_cleaned_history_can_be_removed_with_engine_offline(api, user, engine, monkeypatch):
    identifier, gid, _ = await task_for(user)
    await worker._import(identifier, threading.Event())
    async with SessionLocal() as db:
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == gid).values(state="removed"))
        task = await db.get(OfflineTask, identifier)
        task.desired_state = "removed"
        task.cleanup_pending = True
        await db.commit()

    async def unavailable(*args, **kwargs):
        raise Aria2Error("engine offline")
    monkeypatch.setattr(engine, "status", unavailable)
    monkeypatch.setattr(engine, "call", unavailable)
    await worker.run_once()
    async with SessionLocal() as db:
        task = await db.get(OfflineTask, identifier)
        assert task.removed_at and not task.cleanup_pending


async def test_retried_bt_waits_for_another_owner_of_the_same_hash(api, user, engine):
    first, first_gid, _ = await task_for(user, kind="torrent", state="queued")
    second, second_gid, second_data = await task_for(user, kind="torrent")
    async with SessionLocal() as db:
        await db.execute(update(OfflineTask).where(OfflineTask.id.in_([first, second])).values(info_hash="a" * 40))
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == first_gid).values(state="removed"))
        await db.execute(update(OfflineTransfer).where(OfflineTransfer.gid == second_gid).values(state="active"))
        await db.commit()
    engine.entries[second_gid] = complete_status(second, second_gid, second_data, kind="torrent", state="active")

    await worker._handle(first, await values(), False)

    async with SessionLocal() as db:
        assert (await db.get(OfflineTask, first)).state == "resource_wait"
    assert not any(name == "addTorrent" for name, _ in engine.calls)


async def test_cancellation_collects_children_created_during_the_stop(api, user, engine, monkeypatch):
    identifier, parent, data = await task_for(user, kind="magnet")
    child = uuid4().hex[:16]
    await worker._set_task(identifier, desired_state="cancelled", cleanup_pending=True)
    engine.entries[parent] = {"gid": parent, "status": "active"}
    original = engine.call

    async def racing(method, *args):
        if method == "forceRemove" and args[0] == parent:
            engine.entries[parent] = {"gid": parent, "status": "complete", "followedBy": [child]}
            engine.entries[child] = complete_status(identifier, child, data, kind="torrent", state="paused")
            raise Aria2Error(code=1)
        return await original(method, *args)

    monkeypatch.setattr(engine, "call", racing)
    await worker._handle(identifier, await values(), False)
    assert not engine.entries


async def test_missing_magnet_payload_restarts_with_metadata_role(api, user, engine):
    identifier, old_gid, _ = await task_for(user, kind="magnet", state="queued")
    await worker._handle(identifier, await values(), False)
    async with SessionLocal() as db:
        transfers=list((await db.scalars(select(OfflineTransfer).where(
            OfflineTransfer.task_id==identifier).order_by(OfflineTransfer.id))).all())
        assert transfers[-1].gid!=old_gid and transfers[-1].role=="metadata"
        assert (await db.get(OfflineTask, identifier)).state=="resolving"


async def test_discovering_the_same_magnet_child_twice_is_idempotent(api, user, engine):
    identifier, parent, _=await task_for(user,kind="magnet")
    child=uuid4().hex[:16]
    engine.entries[parent]={"gid":parent,"status":"complete","followedBy":[child]}
    async with SessionLocal() as db:
        transfer=await db.scalar(select(OfflineTransfer).where(OfflineTransfer.gid==parent))
    await worker._register_children((await worker._snapshot(identifier))[0],[transfer])
    await worker._register_children((await worker._snapshot(identifier))[0],[transfer])
    async with SessionLocal() as db:
        transfers=list((await db.scalars(select(OfflineTransfer).where(OfflineTransfer.task_id==identifier))).all())
        assert len(transfers)==2


@pytest.mark.parametrize("failure", [Aria2Error(code=1), Aria2ResponseTooLarge("resource list too large")])
async def test_one_rejected_task_does_not_block_the_scheduler(api,user,engine,monkeypatch,failure):
    first,_,_=await task_for(user,state="queued")
    second,_,_=await task_for(user,state="queued")
    processed=[]
    async def handle(identifier,*args):
        if identifier==first:raise failure
        processed.append(identifier)
    monkeypatch.setattr(worker,"_handle",handle)
    await worker.run_once()
    assert second in processed
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask,first)).state=="error"
    assert (await worker.service_status())["available"] is True


async def test_large_file_lists_do_not_prevent_compact_control_requests(monkeypatch):
    monkeypatch.setattr(settings,"aria2_rpc_secret","isolated-rpc-test")
    monkeypatch.setattr(aria2_rpc,"MAX_RESPONSE_BYTES",256)
    async def handle(request):
        payload=json.loads(request.content)
        result={"status":"active"}
        if "files" in payload["params"][-1]:result["files"]=[{"path":"x"*1024}]
        return httpx.Response(200,json={"result":result})
    client=Aria2RPC(transport=httpx.MockTransport(handle))
    assert (await client.status("a"*16,detailed=False))["status"]=="active"
    with pytest.raises(Aria2ResponseTooLarge):
        await client.status("a"*16)


@pytest.mark.parametrize("lengths", [[2**63], [2**62,2**62]])
async def test_engine_file_sizes_cannot_overflow_database_integers(api,user,engine,lengths):
    identifier,gid,data=await task_for(user,kind="torrent",files=[(f"{i}.bin",b"x") for i in range(len(lengths))])
    status=complete_status(identifier,gid,data,kind="torrent")
    for item,length in zip(status["files"],lengths):item["length"]=str(length)
    with pytest.raises(offline.OfflineError):
        await worker._update_files((await worker._snapshot(identifier))[0],status,await values())


@pytest.mark.parametrize("paths", [[[b"a"],[b"a",b"b"]], [[b"a",b"b"],[b"a"]]])
def test_torrent_rejects_file_directory_collisions(paths):
    with pytest.raises(TorrentError,match="冲突"):
        parse_torrent(torrent(paths=paths))
