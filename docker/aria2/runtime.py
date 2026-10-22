"""Small standard-library helpers shared by the isolated downloader processes."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import urllib.request

DOWNLOADS = Path("/downloads")
CONTROL = DOWNLOADS / "control"
STATE = Path("/state")
SECRET = CONTROL / "rpc-secret"
UID = 10001
WATCHDOG_UID = 10002
GID = 10001


def drop_privileges(uid: int) -> None:
    """Discard supplementary groups, the capability bounding set and setuid gains."""
    if os.getuid() != 0:
        raise RuntimeError("downloader initialization must run as root before dropping privileges")
    libc = ctypes.CDLL(None, use_errno=True)
    last_cap = int(Path("/proc/sys/kernel/cap_last_cap").read_text().strip())
    os.setgroups([GID])
    os.setgid(GID)
    for capability in range(last_cap + 1):
        if libc.prctl(24, capability, 0, 0, 0) != 0:  # PR_CAPBSET_DROP
            raise OSError(ctypes.get_errno(), "cannot clear downloader capability bounding set")
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "cannot set no-new-privileges")
    os.setuid(uid)


def rpc(method: str, params: list | None = None, *, timeout: float = 3.0):
    secret = SECRET.read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError("RPC secret is unavailable")
    payload = json.dumps({"jsonrpc": "2.0", "id": "watchdog", "method": method,
                          "params": ["token:" + secret, *(params or [])]}).encode()
    request = urllib.request.Request("http://127.0.0.1:6800/jsonrpc", data=payload,
                                     headers={"Content-Type": "application/json"})
    # Never inherit a proxy: this call must stay inside the downloader namespace.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError("Aria2 RPC operation failed")
    return result["result"]
