from runtime import WATCHDOG_UID, drop_privileges, rpc

try:
    drop_privileges(WATCHDOG_UID)
    version = rpc("aria2.getVersion")
    raise SystemExit(0 if version.get("version") else 1)
except Exception:
    raise SystemExit(1)
