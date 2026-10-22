"""Compare a controlled allowed endpoint to a controlled protected endpoint."""
import os
from pathlib import Path
import socket

assert os.environ.get("FT_ARIA2_BT_PORT") == "16882", "acceptance container only"
assert os.getuid() == 10001
allowed = socket.create_connection(("source", 8001), timeout=3)
allowed.close()
for host, port in (("source", 8002), ("127.0.0.1", 6800)):
    try:
        connection = socket.create_connection((host, port), timeout=2)
    except OSError:
        print("blocked protected endpoint", host, port)
    else:
        connection.close()
        raise AssertionError("protected endpoint was reachable")
daemons=[]
for process in Path("/proc").glob("[0-9]*"):
    try:
        if (process/"comm").read_text().strip() != "aria2c":
            continue
        status=dict(line.split(":",1) for line in (process/"status").read_text().splitlines() if ":" in line)
    except (FileNotFoundError,PermissionError):
        continue
    assert status["Uid"].split()[0] == "10001"
    for field in ("CapEff","CapPrm","CapBnd"):
        assert int(status[field].strip(),16)==0
    assert status["NoNewPrivs"].strip()=="1"
    daemons.append(process.name)
assert daemons
print("EGRESS + DAEMON UID/CAPABILITIES PASS")
