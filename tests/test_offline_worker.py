"""Exercise engine reconciliation and filesystem/DB boundaries, not implementation strings."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import FileItem, OfflineTask, OfflineTaskFile, OfflineTransfer, User, utcnow
from app.services import offline, offline_storage as disk, offline_worker as worker, settings_service
from app.services.aria2_rpc import Aria2Error, Aria2RPC


class Engine:
    def __init__(self):
        self.entries = {}
        self.calls = []

    async def status(self, gid, *, detailed=True):
        if gid not in self.entries:
            raise Aria2Error(code=1)
        return copy.deepcopy(self.entries[gid])

    async def call(self, method, *args):
        self.calls.append((method, args))
        if method == "getSessionInfo":
            return {"sessionId": "test-session"}
        if method in {"changeGlobalOption", "saveSession", "changeOption"}:
            return "OK"
        if method == "forceRemove":
            self.entries[args[0]]["status"] = "removed"
        elif method == "removeDownloadResult":
            self.entries.pop(args[0], None)
        elif method == "forcePause":
            self.entries[args[0]]["status"] = "paused"
        elif method == "unpause":
            self.entries[args[0]]["status"] = "active"
        elif method == "addUri":
            opts = args[1]
            self.entries[opts["gid"]] = {"gid":opts["gid"],"status":"paused","files":[],"totalLength":"0"}
            return opts["gid"]
        return "OK"


@pytest.fixture
async def engine(transport, monkeypatch):
    fake = Engine()
    monkeypatch.setattr(worker, "rpc", fake)
    # Reconciliation tests supply a healthy network guard with their fake RPC.
    monkeypatch.setattr(worker, "protection_error", lambda _host: None)
    worker._applied = None
    worker._seed_applied.clear()
    # Different tests use different owners. Keep engine reconciliation task-scoped
    # rather than processing all historical test tasks with a fresh fake engine.
    yield fake
    for job, stop in worker._imports.values():
        stop.set()
        job.cancel()
    await asyncio.gather(*(job for job, _ in worker._imports.values()), return_exceptions=True)
    worker._imports.clear()


async def task_for(user, *, kind="http", state="downloading", files=None, selected=True):
    task_id, gid = uuid4().hex, uuid4().hex[:16]
    data = files or [("sample.bin", b"completed-payload")]
    async with SessionLocal() as db:
        task = OfflineTask(id=task_id,user_id=user["id"],kind=kind,name="sample",
            source_encrypted=settings_service.encrypt_secret("https://example.com/sample.bin"),
            state=state,desired_state="running",is_public=False,expires_hours=336,
            reserved_files=len(data) if selected else 0,reserved_bytes=sum(len(b) for _,b in data) if selected else 0,
            total_bytes=sum(len(b) for _,b in data),completed_bytes=0,download_speed=0,upload_speed=0,connections=0,
            cleanup_pending=False,revision=0)
        db.add(task)
        await db.flush()
        db.add(OfflineTransfer(task_id=task_id,gid=gid,role="payload",state="complete"))
        for index,(name,content) in enumerate(data,1):
            db.add(OfflineTaskFile(task_id=task_id,file_index=index,relative_path=name,name=name,
                size=len(content),selected=True,completed_bytes=len(content)))
            path = disk.ensure_task_dir(task_id) / name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content)
        await db.commit()
    return task_id,gid,data


async def values(**updates):
    async with SessionLocal() as db:
        result = dict(await settings_service.all_values(db))
    result.update({"offline.enabled":True,"offline.min_free_mb":0})
    result.update(updates)
    return result


async def test_scheduler_publishes_site_domain_and_blocks_until_guard_ready(api, user, engine, monkeypatch):
    identifier, _, _ = await task_for(user, state="queued")
    async with SessionLocal() as db:
        config = dict(await settings_service.all_values(db, force=True))
    config.update({"site.base_url": "https://PAN.example.com.:8443", "offline.enabled": True})
    async def configured(*args, **kwargs):
        return config
    monkeypatch.setattr(settings_service, "all_values", configured)
    seen = []
    monkeypatch.setattr(worker, "protection_error", lambda host: "网络保护测试暂停" if host == "pan.example.com" else "域名错误")
    async def handle(task_id, _values, blocked):
        seen.append((task_id, blocked))
    monkeypatch.setattr(worker, "_handle", handle)
    await worker.run_once()
    policy = json.loads((settings.offline_root / "control/policy.json").read_text())
    assert policy["site_hostname"] == "pan.example.com"
    assert (identifier, "网络保护测试暂停") in seen
    status = await worker.service_status()
    assert not status["available"] and not status["config_synced"]
    assert status["error"] == "网络保护测试暂停"


def complete_status(task_id,gid,data,*,kind="http",state="complete"):
    result = {"gid":gid,"status":state,"downloadSpeed":"0","uploadSpeed":"4","connections":"1",
        "files":[{"index":str(i),"path":f"/downloads/tasks/{task_id}/payload/{name}","length":str(len(content)),
                  "completedLength":str(len(content)),"selected":"true"} for i,(name,content) in enumerate(data,1)]}
    if kind != "http":
        result["bittorrent"]={"info":{"name":"sample"}}
        result["seeder"]="true"
    return result


async def test_cross_volume_import_private_14_days_and_idempotent(api,user,engine):
    task_id,gid,data = await task_for(user)
    before = utcnow()
    await worker._import(task_id,threading.Event())
    await worker._import(task_id,threading.Event())
    async with SessionLocal() as db:
        task = await db.get(OfflineTask,task_id)
        records = list((await db.scalars(select(FileItem).where(FileItem.user_id==user["id"]))).all())
        assert len(records)==1
        record=records[0]
        assert record.is_public is False
        assert before+timedelta(days=14) <= record.expires_at <= utcnow()+timedelta(days=14)
        assert record.sha256==hashlib.sha256(data[0][1]).hexdigest()
        assert (settings.storage_dir/record.stored_path).read_bytes()==data[0][1]
        assert task.imported_at and task.reserved_bytes==task.reserved_files==0
        owner=await db.get(User,user["id"])
        assert owner.used_files==1 and owner.used_bytes==len(data[0][1])
    engine.entries[gid]=complete_status(task_id,gid,data)
    await worker._cleanup(task_id)
    assert not disk.task_dir(task_id).exists()
    assert (await api.get(f'/f/{record.public_id}')).status_code==200


async def test_batch_copy_failure_exposes_no_partial_files(api,user,engine):
    task_id,_,data=await task_for(user,kind="torrent",files=[("a.txt",b"one"),("b.txt",b"two")])
    (disk.payload_dir(task_id)/"b.txt").unlink()
    await worker._import(task_id,threading.Event())
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FileItem).where(FileItem.user_id==user["id"]))==0
        task=await db.get(OfflineTask,task_id)
        assert task.state=="error" and not task.imported_at
    (disk.payload_dir(task_id)/"b.txt").write_bytes(b"two")
    await worker._import(task_id,threading.Event())
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FileItem).where(FileItem.user_id==user["id"]))==2
        owner=await db.get(User,user["id"])
        assert owner.used_files==2 and owner.used_bytes==6


async def test_bt_stays_paused_until_selection_even_after_resolving_pause(api,user,engine):
    task_id,gid,data=await task_for(user,kind="magnet",state="queued",selected=False)
    engine.entries[gid]=complete_status(task_id,gid,data,kind="torrent",state="paused")
    await worker._handle(task_id,await values(),False)
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,task_id)
        assert task.state=="awaiting_selection" and task.reserved_files==0
    assert engine.entries[gid]["status"]=="paused"
    assert not any(name=="unpause" for name,_ in engine.calls)


async def test_magnet_metadata_error_is_not_infinite_resolving(api,user,engine):
    task_id,gid,data=await task_for(user,kind="magnet",state="resolving",selected=False)
    async with SessionLocal() as db:
        tr=await db.scalar(select(OfflineTransfer).where(OfflineTransfer.gid==gid))
        tr.role="metadata"
        await db.commit()
    engine.entries[gid]={"gid":gid,"status":"error","errorCode":"1","files":[]}
    await worker._handle(task_id,await values(),False)
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask,task_id)).state=="error"


async def test_seeding_source_retained_then_admin_stops_without_deleting_saved_file(api,user,engine):
    task_id,gid,data=await task_for(user,kind="torrent")
    engine.entries[gid]=complete_status(task_id,gid,data,kind="torrent",state="active")
    await worker._import(task_id,threading.Event())
    v=await values(**{"offline.seed_enabled":True})
    await worker._handle(task_id,v,False)
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,task_id)
        assert task.state=="seeding" and task.seed_started_at
        mapped=await db.scalar(select(OfflineTaskFile).where(OfflineTaskFile.task_id==task_id))
        saved=await db.get(FileItem,mapped.file_id)
        saved_path=settings.storage_dir/saved.stored_path
    assert disk.payload_dir(task_id).exists()
    v["offline.seed_enabled"]=False
    await worker._handle(task_id,v,False)
    assert not disk.task_dir(task_id).exists()
    assert saved_path.read_bytes()==data[0][1]
    assert gid not in engine.entries


async def test_deleted_file_is_not_reimported_on_recovery(api,user,engine):
    task_id,gid,data=await task_for(user,kind="torrent")
    engine.entries[gid]=complete_status(task_id,gid,data,kind="torrent",state="active")
    await worker._import(task_id,threading.Event())
    async with SessionLocal() as db:
        mapped=await db.scalar(select(OfflineTaskFile).where(OfflineTaskFile.task_id==task_id))
        file_id=mapped.file_id
    assert (await api.delete(f"/api/files/{file_id}")).status_code==200
    await worker._handle(task_id,await values(**{"offline.seed_enabled":True}),False)
    await worker._import(task_id,threading.Event())
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FileItem).where(FileItem.user_id==user["id"]))==0
        mapped=await db.get(OfflineTaskFile,mapped.id)
        assert mapped.imported_at and mapped.file_id is None


async def test_cancel_stops_all_child_gids_before_source_cleanup(api,user,engine):
    task_id,gid,data=await task_for(user,kind="magnet")
    child=uuid4().hex[:16]
    engine.entries[gid]={"gid":gid,"status":"complete","followedBy":[child]}
    engine.entries[child]=complete_status(task_id,child,data,kind="torrent",state="active")
    await worker._set_task(task_id,desired_state="cancelled",cleanup_pending=True)
    await worker._handle(task_id,await values(),False)
    assert not engine.entries and not disk.task_dir(task_id).exists()
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,task_id)
        assert task.state=="cancelled" and task.reserved_bytes==0


@pytest.mark.parametrize("http_status", [200, 400])
async def test_rpc_never_exposes_remote_signed_uri_or_secret(monkeypatch, http_status):
    monkeypatch.setattr(settings,"aria2_rpc_secret","do-not-leak-secret")
    async def handle(request):
        content=json.loads(request.content)
        assert content["params"][0]=="token:do-not-leak-secret"
        return httpx.Response(http_status,json={"error":{"code":1,"message":"https://host/file?passkey=secret"}})
    client=Aria2RPC(transport=httpx.MockTransport(handle))
    with pytest.raises(Aria2Error) as exc:
        await client.call("tellStatus","0"*16)
    assert "passkey" not in str(exc.value) and "do-not-leak" not in str(exc.value)
    assert exc.value.code == 1


async def test_http_without_headers_can_start_with_conservative_reservation(api, user, engine):
    task_id, gid, _ = await task_for(user, state="queued")
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,task_id)
        task.reserved_bytes=10000
        task.total_bytes=0
        item=await db.scalar(select(OfflineTaskFile).where(OfflineTaskFile.task_id==task_id))
        item.size=item.completed_bytes=0
        await db.commit()
    engine.entries[gid]={"gid":gid,"status":"paused","files":[{
        "index":"1","path":"","length":"0","completedLength":"0","selected":"true"}]}
    await worker._handle(task_id,await values(),False)
    assert engine.entries[gid]["status"]=="active"
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,task_id)
        assert task.state=="downloading" and task.reserved_bytes==10000
        assert task.imported_at is None


async def test_bt_result_with_discarded_file_counters_still_imports(api, user, engine):
    task_id,gid,data=await task_for(user,kind="torrent")
    status=complete_status(task_id,gid,data,kind="torrent")
    status.update(totalLength=str(sum(len(content) for _,content in data)),
                  completedLength=str(sum(len(content) for _,content in data)))
    for item in status["files"]:
        item["completedLength"]="0"
    engine.entries[gid]=status
    await worker._handle(task_id,await values(),False)
    assert task_id in worker._imports
    await worker._imports[task_id][0]
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask,task_id)).imported_at is not None
