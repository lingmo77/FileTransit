"""Optional public HTTPS smoke test; run only in the isolated acceptance app."""
import asyncio
import hashlib
import json
import httpx
from sqlalchemy import select
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import User,OfflineTask,OfflineTaskFile,FileItem
from app.services import offline

assert settings.secret_key=='isolated-offline-acceptance-secret-only'

async def main():
    url='https://www.python.org/robots.txt'
    async with httpx.AsyncClient(trust_env=False,timeout=30) as client:
        response=await client.get(url)
        response.raise_for_status()
        expected=hashlib.sha256(response.content).hexdigest()
    async with SessionLocal() as db:
        owner=await db.scalar(select(User).where(User.username=='labadmin'))
        task=await offline.create_link_task(db,owner,url,is_public=True)
        identifier=task.id
    for _ in range(180):
        async with SessionLocal() as db:
            task=await db.get(OfflineTask,identifier)
            if task.imported_at:
                mapped=await db.scalar(select(OfflineTaskFile).where(OfflineTaskFile.task_id==identifier))
                record=await db.get(FileItem,mapped.file_id)
                assert record.is_public and record.sha256==expected
                assert hashlib.sha256((settings.storage_dir/record.stored_path).read_bytes()).hexdigest()==expected
                public_id=record.public_id
                break
            if task.state=='error':raise AssertionError(task.error)
        await asyncio.sleep(1)
    else:raise AssertionError('HTTPS smoke timeout')
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8000') as client:
        detail=await client.get('/f/'+public_id)
        assert detail.status_code==200
    print(json.dumps({'https_tls_and_checksum':'passed','public_file_anonymous_detail':'passed'}),flush=True)

asyncio.run(main())
