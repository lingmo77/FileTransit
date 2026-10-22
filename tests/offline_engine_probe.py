"""Run as watchdog UID, only inside the new isolated acceptance downloader."""
import json
import os
from pathlib import Path
import shutil
import sys
import time
import base64
import urllib.request
from urllib.parse import urlencode

sys.path.insert(0,"/opt/aria2")
from runtime import rpc

task_id="f"*32
base=Path("/downloads/tasks")/task_id
base.mkdir(mode=0o770,exist_ok=True)
base.chmod(0o770)
payload=base/"payload"
payload.mkdir(mode=0o770,exist_ok=True)
payload.chmod(0o770)
policy={"temp_max_bytes":2*1024**3,"min_free_bytes":0,
        "tasks":{task_id:{"max_bytes":5*1024**2,"desired_state":"running"}}}
Path("/downloads/control/policy.json").write_text(json.dumps(policy))
time.sleep(3)
print("engine",rpc("aria2.getVersion")["version"],flush=True)
rpc("aria2.changeGlobalOption",[{"seed-time":"0","seed-ratio":"1.0",
    "max-overall-download-limit":"0","max-overall-upload-limit":"0","max-concurrent-downloads":"3"}])
gid=rpc("aria2.addUri",[["http://source:8001/single.bin"],{"dir":str(payload),"pause":"true",
    "pause-metadata":"true","continue":"true","check-integrity":"true","follow-torrent":"false"}])
rpc("aria2.changeOption",[gid,{"seed-time":"0","seed-ratio":"1.0"}])
rpc("aria2.unpause",[gid])
for _ in range(40):
    status=rpc("aria2.tellStatus",[gid])
    if status["status"] in {"complete","error"}:
        break
    time.sleep(.5)
print(json.dumps({"status":status["status"],"errorCode":status.get("errorCode"),
    "length":status.get("totalLength"),"files":len(status.get("files",[]))}),flush=True)
assert status["status"]=="complete"
assert (payload/"single.bin").read_bytes()==bytes(range(256))*512
rpc("aria2.removeDownloadResult",[gid])
rpc("aria2.saveSession")
assert base.resolve().parent==Path("/downloads/tasks").resolve() and base.name=="f"*32
shutil.rmtree(base)
print("RAW ENGINE HTTP/CHECKSUM/OPTIONS PASS",flush=True)

data=urllib.request.urlopen("http://source:8001/fixture.torrent").read()
def prepare(task_id):
    directory=Path("/downloads/tasks")/task_id
    directory.mkdir(mode=0o770,exist_ok=True)
    directory.chmod(0o770)
    payload=directory/"payload"
    payload.mkdir(mode=0o770,exist_ok=True)
    payload.chmod(0o770)
    policy["tasks"]={task_id:{"max_bytes":5*1024**2,"desired_state":"running"}}
    Path("/downloads/control/policy.json").write_text(json.dumps(policy))
    time.sleep(2)
    return directory,payload

def wait(gid):
    for _ in range(120):
        status=rpc("aria2.tellStatus",[gid])
        if status["status"] in {"complete","error"}:
            break
        time.sleep(.5)
    print(json.dumps({"status":status["status"],"errorCode":status.get("errorCode"),
        "complete":status.get("completedLength"),"total":status.get("totalLength")}),flush=True)
    assert status["status"]=="complete",status.get("errorMessage")
    return status

base,payload=prepare("e"*32)
gid=rpc("aria2.addTorrent",[base64.b64encode(data).decode(),[],{"dir":str(payload),"select-file":"2",
    "pause":"true","check-integrity":"true","seed-time":"0"}])
rpc("aria2.unpause",[gid])
status=wait(gid)
info_hash=status["infoHash"]
assert (payload/"fixture"/"two.bin").read_bytes()==b"offline-fixture-data"*4096
rpc("aria2.removeDownloadResult",[gid])
rpc("aria2.saveSession")
print("RAW TORRENT SELECTION/CHECKSUM PASS",flush=True)

base,payload=prepare("d"*32)
magnet="magnet:?"+urlencode({"xt":"urn:btih:"+info_hash,"tr":"http://source:8001/announce","dn":"fixture"})
parent=rpc("aria2.addUri",[[magnet],{"dir":str(payload),"pause":"false","pause-metadata":"true","seed-time":"0"}])
for _ in range(120):
    meta=rpc("aria2.tellStatus",[parent])
    if meta.get("followedBy"):
        break
    if meta["status"]=="error":
        raise AssertionError("magnet metadata error "+meta.get("errorCode",""))
    time.sleep(.5)
assert meta.get("followedBy"),"magnet metadata timeout"
child=meta["followedBy"][0]
assert rpc("aria2.tellStatus",[child])["status"]=="paused"
rpc("aria2.changeOption",[child,{"select-file":"1,2","seed-time":"0"}])
rpc("aria2.unpause",[child])
wait(child)
assert (payload/"fixture"/"one.bin").read_bytes()==bytes(range(256))*512
for gid in (child,parent):
    rpc("aria2.removeDownloadResult",[gid])
rpc("aria2.saveSession")
print("RAW MAGNET METADATA/PAUSE/CHILD/CHECKSUM PASS",flush=True)
