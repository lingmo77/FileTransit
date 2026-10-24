"""Initialize the private volume/firewall, then supervise a capability-free Aria2."""
from __future__ import annotations

import os
from pathlib import Path
import json
import secrets
import signal
import subprocess
import sys
import time

from firewall import install
from runtime import (CONTROL, DOWNLOADS, GID, SECRET, STATE, UID, WATCHDOG_UID,
                     drop_privileges, network_guard_privileges, rpc)
from watchdog import inspect_once

stopping = False


def stop(_signum, _frame):
    global stopping
    stopping = True


def prepare() -> Path:
    os.umask(0o027)
    for directory in (DOWNLOADS, CONTROL, DOWNLOADS / "tasks", STATE):
        if directory.is_symlink():
            raise RuntimeError("downloader volume path cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, UID, GID)
        directory.chmod(0o770)
    if SECRET.is_symlink():
        raise RuntimeError("RPC secret cannot be a symlink")
    if not SECRET.exists():
        with SECRET.open("x", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
    token = SECRET.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(ch.isspace() for ch in token):
        raise RuntimeError("persisted RPC secret is invalid")
    os.chown(SECRET, UID, GID)
    SECRET.chmod(0o640)
    session = STATE / "aria2.session"
    if session.is_symlink():
        raise RuntimeError("session path cannot be a symlink")
    session.touch(exist_ok=True)
    os.chown(session, UID, GID)
    session.chmod(0o640)
    port = int(os.environ.get("FT_ARIA2_BT_PORT", "16881"))
    if not 1024 <= port <= 65535 or port == 6800:
        raise ValueError("BT port must be 1024..65535 and different from RPC")
    config = STATE / "aria2.conf"
    if config.is_symlink():
        raise RuntimeError("configuration path cannot be a symlink")
    values = {
        "enable-rpc": "true", "rpc-listen-all": "true", "rpc-listen-port": "6800",
        "rpc-secret": token, "rpc-allow-origin-all": "false", "rpc-max-request-size": "48M",
        "dir": "/downloads/tasks", "input-file": str(session), "save-session": str(session),
        "save-session-interval": "5", "force-save": "true", "pause": "true",
        "pause-metadata": "true", "rpc-save-upload-metadata": "true",
        "dht-file-path": "/state/dht.dat", "dht-file-path6": "/state/dht6.dat",
        "enable-dht": "true", "enable-dht6": "true", "enable-peer-exchange": "true",
        "bt-enable-lpd": "false", "bt-save-metadata": "true", "bt-load-saved-metadata": "true",
        "listen-port": str(port), "dht-listen-port": str(port),
        "bt-detach-seed-only": "true",
        "seed-time": "0", "seed-ratio": "1.0", "check-integrity": "true",
        "file-allocation": "none", "auto-file-renaming": "false", "allow-overwrite": "false",
        "allow-piece-length-change": "false", "follow-metalink": "false",
        "max-concurrent-downloads": "3", "max-overall-download-limit": "0",
        "max-overall-upload-limit": "0", "max-upload-limit": "0",
        "max-connection-per-server": "4", "split": "4", "max-tries": "5",
        "retry-wait": "5", "connect-timeout": "15", "timeout": "60",
        "bt-tracker-connect-timeout": "10", "bt-tracker-timeout": "30",
        "console-log-level": "warn", "summary-interval": "0", "enable-color": "false",
        "download-result": "hide", "log": "", "max-download-result": "2000",
    }
    config.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    os.chown(config, UID, GID)
    config.chmod(0o600)
    install()
    return config


def main() -> int:
    config = prepare()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    guard = subprocess.Popen([sys.executable, str(Path(__file__).with_name("network_guard.py"))],
                             preexec_fn=network_guard_privileges,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = subprocess.Popen(["aria2c", "--conf-path=" + str(config)],
                             preexec_fn=lambda: drop_privileges(UID),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    drop_privileges(WATCHDOG_UID)
    last_warning = None
    try:
        while child.poll() is None and not stopping:
            if guard.poll() is not None:
                # Exiting namespace PID 1 also terminates the downloader.
                print("[aria2] network protection exited; downloader stopping", flush=True)
                return 1
            try:
                warning = inspect_once()
                state = CONTROL / "watchdog-status.json"
                temporary = CONTROL / "watchdog-status.next"
                temporary.write_text(json.dumps({"blocked": bool(warning), "reason": warning,
                                                  "updated_at": time.time()}), encoding="utf-8")
                temporary.chmod(0o640)
                temporary.replace(state)
                if warning != last_warning:
                    if warning:
                        print("[disk-watchdog] " + warning, flush=True)
                    last_warning = warning
            except Exception:
                # Never log RPC payloads, tokens, URI query strings or trackers.
                pass  # Aria2 may still be starting; keep the supervisor alive.
            time.sleep(2)
        if stopping and child.poll() is None:
            try:
                rpc("aria2.saveSession")
                rpc("aria2.shutdown")
            except Exception:
                pass
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # As namespace PID 1, exiting terminates remaining children.
                return 1
        if child.returncode:
            print("[aria2] downloader exited with status " + str(child.returncode), flush=True)
        return child.returncode or 0
    finally:
        if child.poll() is not None:
            child.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("[aria2] initialization failed; downloader remains closed", file=sys.stderr, flush=True)
        sys.exit(1)
