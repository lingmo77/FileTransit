"""Read-only diagnostics for the explicitly isolated acceptance app."""
import asyncio
import json
import sys
from sqlalchemy import select
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import OfflineTask, OfflineTransfer
from app.services.aria2_rpc import rpc

assert settings.secret_key == "isolated-offline-acceptance-secret-only"

async def main():
    if "--probe" in sys.argv:
        gid=await rpc.call("addUri",["http://source:8001/single.bin"],{
            "dir":"/downloads/tasks/"+"c"*32+"/payload","pause":"true"})
        print("INITIAL_HTTP",json.dumps(await rpc.status(gid)),flush=True)
        await rpc.call("forceRemove",gid)
        await rpc.call("removeDownloadResult",gid)
    for method in ("getSessionInfo", "getGlobalStat"):
        try:
            result = await rpc.call(method)
            print(method, "PASS", flush=True)
        except Exception as error:
            print(method, type(error).__name__, str(error), flush=True)
    async with SessionLocal() as db:
        for task in await db.scalars(select(OfflineTask)):
            print(json.dumps({"task":task.id,"state":task.state,"desired":task.desired_state,
                "error":task.error,"size":task.total_bytes,"done":task.completed_bytes,
                "reserved":task.reserved_bytes},ensure_ascii=False),flush=True)
            for transfer in await db.scalars(select(OfflineTransfer).where(OfflineTransfer.task_id==task.id)):
                try:
                    status=await rpc.status(transfer.gid)
                    result={key:status.get(key) for key in ("gid","status","completedLength","totalLength","errorCode","followedBy","verifiedLength","verifyIntegrityPending")}
                    result['present_keys']=list(status)
                    result['files']=[{key:file.get(key) for key in ('index','path','length','completedLength','selected')} for file in status.get('files',[])]
                    print(json.dumps(result),flush=True)
                except Exception as error:
                    print("status",type(error).__name__,str(error),flush=True)

asyncio.run(main())
