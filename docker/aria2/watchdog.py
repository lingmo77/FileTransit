"""Independent soft disk protection, including while the web app is restarting."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat

from runtime import CONTROL, DOWNLOADS, rpc

TASK_ID = re.compile(r"^[0-9a-f]{32}$")


def occupied_bytes(directory: Path) -> int:
    """Count allocated bytes without following task-controlled links."""
    total = 0
    def unreadable(error):
        raise error

    for current, dirs, files in os.walk(directory, followlinks=False, onerror=unreadable):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            try:
                info = (Path(current) / name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += getattr(info, "st_blocks", (info.st_size + 511) // 512) * 512
    return total


def policy() -> dict:
    data = (CONTROL / "policy.json").read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("watchdog policy is too large")
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict):
        raise ValueError("watchdog policy is invalid")
    for key in ("temp_max_bytes", "min_free_bytes"):
        if isinstance(value.get(key), bool) or not isinstance(value.get(key), int) or value[key] < 0:
            raise ValueError("watchdog disk budget is invalid")
    if value["temp_max_bytes"] == 0:
        raise ValueError("watchdog requires a bounded temporary budget")
    return value


def inspect_once() -> str | None:
    try:
        limits = policy()
    except (OSError, ValueError, TypeError):
        rpc("aria2.forcePauseAll")
        return "waiting for a valid disk policy"
    active = rpc("aria2.tellActive", [["gid", "dir", "status"]])
    try:
        used = occupied_bytes(DOWNLOADS / "tasks")
    except OSError:
        rpc("aria2.forcePauseAll")
        return "temporary disk usage cannot be verified"
    free = shutil.disk_usage(DOWNLOADS).free
    # Leave room for bytes arriving between checks. This is a soft safeguard,
    # not a filesystem/project quota and not a promise of an exact hard limit.
    margin = min(64 * 1024**2, max(1024**2, limits["temp_max_bytes"] // 100))
    if used + margin >= limits["temp_max_bytes"] or free <= limits["min_free_bytes"] + margin:
        rpc("aria2.forcePauseAll")
        return "temporary space budget or disk reserve reached"
    for transfer in active:
        try:
            relative = Path(transfer["dir"]).relative_to(DOWNLOADS / "tasks")
            task_id = relative.parts[0]
            if not TASK_ID.fullmatch(task_id):
                raise ValueError
        except (KeyError, ValueError, IndexError):
            rpc("aria2.forcePause", [transfer["gid"]])
            continue
        entry = limits["tasks"].get(task_id)
        if not isinstance(entry, dict) or entry.get("desired_state") != "running":
            rpc("aria2.forcePause", [transfer["gid"]])
            continue
        maximum = entry.get("max_bytes")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            rpc("aria2.forcePause", [transfer["gid"]])
            continue
        # BT may allocate boundary pieces in unselected adjacent files; the
        # coordinator includes that overhead in this per-task disk budget.
        try:
            task_bytes = occupied_bytes(DOWNLOADS / "tasks" / task_id)
        except OSError:
            rpc("aria2.forcePauseAll")
            return "temporary disk usage cannot be verified"
        if task_bytes > maximum + margin:
            rpc("aria2.forcePause", [transfer["gid"]])
            return "task temporary budget reached"
    return None
