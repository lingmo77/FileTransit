"""Second-pass business acceptance; ONLY in the isolated ft-offline-check app."""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import OfflineTask, OfflineTransfer, User
from app.services import offline, settings_service
from app.services.aria2_rpc import rpc

assert settings.secret_key == "isolated-offline-acceptance-secret-only"


async def wait(identifier, predicate, seconds=100):
    for _ in range(seconds*2):
        async with SessionLocal() as db:
            task=await db.get(OfflineTask,identifier)
            if predicate(task):return task
            if task.state=="error":raise AssertionError(task.error)
        await asyncio.sleep(.5)
    raise AssertionError(f"timeout: {identifier} {task.state} {task.error}")


async def action(owner_id,identifier,name):
    async with SessionLocal() as db:
        await offline.action_task(db,await db.get(User,owner_id),identifier,name)


async def create_http(owner_id,path):
    original=offline.validate_http_url
    async def controlled(url):
        assert url=="http://source:8001/"+path
        return url
    offline.validate_http_url=controlled
    try:
        async with SessionLocal() as db:
            task=await offline.create_link_task(db,await db.get(User,owner_id),"http://source:8001/"+path)
            return task.id
    finally:offline.validate_http_url=original


async def create_bt(owner_id,data,indexes):
    async with SessionLocal() as db:
        owner=await db.get(User,owner_id)
        task=await offline.create_torrent_task(db,owner,data)
        await offline.select_files(db,owner,task.id,indexes)
        return task.id


async def main():
    async with SessionLocal() as db:
        owner=await db.scalar(select(User).where(User.username=="labadmin"))
        owner.must_change_password=False
        owner_id=owner.id
        await db.commit()
        await settings_service.set_many(db,{"offline.enabled":True,"offline.seed_enabled":False,
            "offline.global_concurrency":3,"offline.user_concurrency":1,"offline.min_free_mb":0,
            "offline.download_limit_mbps":.125,"offline.upload_limit_mbps":0})
    first=await create_http(owner_id,"slow.bin")
    await wait(first,lambda t:t.completed_bytes>=65536)
    await action(owner_id,first,"pause")
    await wait(first,lambda t:t.state=="paused")
    second=await create_http(owner_id,"slow.bin")
    await wait(second,lambda t:t.completed_bytes>=65536)
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask,first)).state=="paused"
    for identifier in (first,second):
        await action(owner_id,identifier,"cancel")
        await wait(identifier,lambda t:t.state=="cancelled" and not t.cleanup_pending)
    print("PAUSED_TASK_RELEASES_SLOT PASS",flush=True)

    async with httpx.AsyncClient() as client:
        response=await client.get("http://source:8001/fixture.torrent")
        response.raise_for_status()
        torrent=response.content
    async with SessionLocal() as db:
        await settings_service.set_many(db,{"offline.seed_enabled":True,"offline.seed_minutes":60,
            "offline.global_concurrency":1,"offline.download_limit_mbps":0})
    seed_id=await create_bt(owner_id,torrent,[1,2])
    await wait(seed_id,lambda t:t.state=="seeding")
    assert (await rpc.call("getGlobalOption"))["bt-detach-seed-only"]=="true"
    retry_id=await create_bt(owner_id,torrent,[1])
    await wait(retry_id,lambda t:t.state=="resource_wait")
    async with SessionLocal() as db:
        task=await db.get(OfflineTask,retry_id)
        task.state="error"
        db.add(OfflineTransfer(task_id=retry_id,gid=uuid4().hex[:16],role="payload",state="removed"))
        await db.commit()
    await action(owner_id,retry_id,"retry")
    await wait(retry_id,lambda t:t.state=="resource_wait")
    http_id=await create_http(owner_id,"single.bin")
    await wait(http_id,lambda t:t.imported_at is not None,seconds=30)
    async with SessionLocal() as db:
        assert (await db.get(OfflineTask,seed_id)).state=="seeding"
        await settings_service.set_many(db,{"offline.seed_enabled":False})
    await wait(seed_id,lambda t:t.state=="saved" and not t.cleanup_pending)
    await wait(retry_id,lambda t:t.imported_at is not None)
    await wait(retry_id,lambda t:t.state=="saved" and not t.cleanup_pending)
    print("SEEDING_WITH_GLOBAL_AND_USER_LIMIT_ONE PASS",flush=True)
    print("SAME_HASH_RETRY_QUEUES_THEN_RECOVERS PASS",flush=True)

    async with SessionLocal() as db:
        await settings_service.set_many(db,{"offline.metadata_max_mb":16})
    # Other HTTP workers refresh their settings cache within fifteen seconds.
    await asyncio.sleep(16)
    large=torrent.replace(b"4:info",b"7:comment8388608:"+b"x"*(8*1024**2)+b"4:info",1)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=60) as client:
        page=await client.get("/login")
        csrf=re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1)
        headers={"X-CSRF-Token":csrf}
        response=await client.post("/api/auth/login",headers=headers,json={"login":"labadmin","password":"lab-offline-password-2026"})
        assert response.status_code==200,response.text
        response=await client.post("/api/offline/tasks/torrent",headers=headers,files={"file":("large.torrent",large)},data={"expires_hours":"336","is_public":"false"})
        assert response.status_code==201,response.text
        identifier=response.json()["task"]["id"]
        response=await client.post(f"/api/offline/tasks/{identifier}/select",headers=headers,json={"indexes":[2]})
        assert response.status_code==200,response.text
    await wait(identifier,lambda t:t.imported_at is not None)
    await wait(identifier,lambda t:t.state=="saved" and not t.cleanup_pending)
    print(json.dumps({"large_torrent_bytes":len(large),"rpc_request_over_10MiB":"PASS"}),flush=True)
    print("SECOND_PASS_ACCEPTANCE PASS",flush=True)


asyncio.run(main())
