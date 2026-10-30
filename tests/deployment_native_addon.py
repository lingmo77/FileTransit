"""Configure only the release's disposable native lab for the documented addon test."""
import hashlib
import http.cookiejar
import json
from pathlib import Path
import sys
import urllib.request

root = Path("/opt/filetransit-140-native/file-transfer")
env_path = root / ".env"
env = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
receipt_path = root / ".acceptance.json"
receipt = json.loads(receipt_path.read_text())
if sys.argv[1] == "prepare":
    assert receipt["env"] == hashlib.sha256(env_path.read_bytes()).hexdigest()
    assert "FT_ARIA2_RPC_URL" not in env
    with env_path.open("a") as output:
        output.write(f"FT_OFFLINE_DIR={root}/data/offline\n"
                     "FT_ARIA2_RPC_URL=http://127.0.0.1:6800/jsonrpc\n"
                     f"FT_ARIA2_RPC_SECRET_FILE={root}/data/offline/control/rpc-secret\n"
                     "FT_ARIA2_BT_PORT=18882\n")
    receipt["env"] = hashlib.sha256(env_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    print("Native addon configuration prepared without changing credentials")
elif sys.argv[1] == "enable":
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
    client.open("http://127.0.0.1:18001/login").read()
    token = next(c.value for c in jar if c.name == "ft_csrf")
    for path, payload in [("/api/auth/login", {"login": "admin", "password": "Lab-" + hashlib.sha256(env["FT_ADMIN_PASSWORD"].encode()).hexdigest()[:24]}),
                          ("/admin/api/settings", {"offline.enabled": True})]:
        req = urllib.request.Request("http://127.0.0.1:18001" + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "X-CSRF-Token": token})
        assert client.open(req).status == 200
    print("Native addon enabled through admin API")
else:
    raise ValueError("prepare or enable required")
