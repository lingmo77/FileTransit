"""Real engine acceptance. Run ONLY in the ft-offline-check app container."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import FileItem, OfflineTask, OfflineTaskFile, User
from app.services import offline, settings_service
from app.services.aria2_rpc import rpc


async def wait_task(task_id, predicate, seconds=100):
    for _ in range(seconds*2):
        async with SessionLocal() as db:
            task = await db.get(OfflineTask, task_id)
            if predicate(task):
                print(json.dumps({"task":task_id,"state":task.state},ensure_ascii=False),flush=True)
                return task
            if task.state == "error":
                raise AssertionError(f"task failed: {task.error}")
        await asyncio.sleep(.5)
    raise AssertionError(f"task timeout {task_id}, state={task.state}, error={task.error}")


async def create_http(owner_id, path):
    original = offline.validate_http_url
    async def controlled(value):
        if value != "http://source:8001/" + path:
            raise ValueError("only the exact lab fixture may bypass public-address validation")
        return value
    offline.validate_http_url = controlled
    try:
        async with SessionLocal() as db:
            owner=await db.get(User,owner_id)
            task=await offline.create_link_task(db,owner,"http://source:8001/"+path)
            return task.id
    finally:
        offline.validate_http_url=original


async def main():
    # This named test admin is created only in a dedicated acceptance data volume.
    async with SessionLocal() as db:
        owner=await db.scalar(select(User).where(User.username=="labadmin"))
        assert owner is not None and settings.secret_key=="isolated-offline-acceptance-secret-only"
        owner.must_change_password=False
        owner_id=owner.id
        await db.commit()
        await settings_service.set_many(db,{"offline.enabled":True,"offline.min_free_mb":0,
            "offline.temp_max_mb":2048,"quota.max_upload_mb":100,"quota.default_max_files":100,
            "offline.seed_enabled":False,"offline.download_limit_mbps":0,"offline.upload_limit_mbps":0})
    print("engine", (await rpc.call("getVersion"))["version"],flush=True)
    async with httpx.AsyncClient() as client:
        torrent=(await client.get("http://source:8001/fixture.torrent")).content
    http_id=await create_http(owner_id,"single.bin")
    await wait_task(http_id,lambda t:t.imported_at is not None)
    async with SessionLocal() as db:
        mapped=await db.scalar(select(OfflineTaskFile).where(OfflineTaskFile.task_id==http_id))
        record=await db.get(FileItem,mapped.file_id)
        assert not record.is_public and (record.expires_at-record.created_at).total_seconds()>13.99*86400
        assert (settings.storage_dir/record.stored_path).read_bytes()==bytes(range(256))*512
    print("HTTP private/14days/checksum PASS",flush=True)
    async with SessionLocal() as db:
        owner=await db.get(User,owner_id)
        task=await offline.create_torrent_task(db,owner,torrent)
        torrent_id=task.id
        await offline.select_files(db,owner,torrent_id,[2])
    await wait_task(torrent_id,lambda t:t.imported_at is not None)
    await wait_task(torrent_id,lambda t:t.state=="saved" and not t.cleanup_pending)
    async with SessionLocal() as db:
        mapped=list((await db.scalars(select(OfflineTaskFile).where(OfflineTaskFile.task_id==torrent_id))).all())
        assert [f.file_index for f in mapped if f.file_id]==[2]
        record=await db.get(FileItem,next(f.file_id for f in mapped if f.file_id))
        assert (settings.storage_dir/record.stored_path).read_bytes()==b"offline-fixture-data"*4096
        info_hash=(await db.get(OfflineTask,torrent_id)).info_hash
    print("TORRENT selected file/checksum PASS",flush=True)
    async with SessionLocal() as db:
        owner=await db.get(User,owner_id)
        magnet="magnet:?"+urlencode({"xt":"urn:btih:"+info_hash,"tr":"http://source:8001/announce","dn":"fixture"})
        task=await offline.create_link_task(db,owner,magnet)
        magnet_id=task.id
    await wait_task(magnet_id,lambda t:t.state=="awaiting_selection")
    async with SessionLocal() as db:
        owner=await db.get(User,owner_id)
        await offline.select_files(db,owner,magnet_id,[1,2])
    await wait_task(magnet_id,lambda t:t.imported_at is not None)
    await wait_task(magnet_id,lambda t:t.state=="saved" and not t.cleanup_pending)
    print("MAGNET metadata/selection/import PASS",flush=True)
    async with SessionLocal() as db:
        await settings_service.set_many(db,{"offline.seed_enabled":True,"offline.seed_minutes":60,
            "offline.download_limit_mbps":.5,"offline.upload_limit_mbps":.125})
        owner=await db.get(User,owner_id)
        task=await offline.create_torrent_task(db,owner,torrent)
        seed_id=task.id
        await offline.select_files(db,owner,seed_id,[1,2])
    await wait_task(seed_id,lambda t:t.state=="seeding")
    options=await rpc.call("getGlobalOption")
    assert options["max-overall-download-limit"]=="524288"
    assert options["max-overall-upload-limit"]=="131072"
    async with SessionLocal() as db:
        await settings_service.set_many(db,{"offline.seed_enabled":False})
    await wait_task(seed_id,lambda t:t.state=="saved" and not t.cleanup_pending)
    print("SEED policy + total download/upload limits PASS",flush=True)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
        page=await client.get("/login")
        csrf=re.search(r'name="csrf-token" content="([^"]+)"',page.text).group(1)
        auth=await client.post("/api/auth/login",headers={"X-CSRF-Token":csrf},json={"login":"labadmin","password":"lab-offline-password-2026"})
        assert auth.status_code==200,auth.text
        for n in range(2):
            response=await client.post("/api/files",headers={"X-CSRF-Token":csrf},
                files={"file":(f"continuous-{n}.bin",b"u"*(1024**2),"application/octet-stream")},
                data={"expires_hours":"336","is_public":"false"})
            assert response.status_code==201,response.text
        result=await client.get("/api/offline/tasks")
        assert result.status_code==200 and "source_encrypted" not in result.text
    print("CONTINUOUS upload + private task API PASS",flush=True)
    print("ACCEPTANCE PASS",flush=True)


asyncio.run(main())
