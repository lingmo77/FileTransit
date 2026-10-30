"""Real installer acceptance; only point at this release's isolated lab directories."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--edition", choices=["bundled", "native"], required=True)
    parser.add_argument("--with-downloader", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    assert str(root).startswith("/opt/filetransit-140-") and root.name == "file-transfer"
    env = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
    assert len(env["FT_SECRET_KEY"]) == 96 and len(env["FT_ADMIN_PASSWORD"]) == 48
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))

    def request(path, payload=None, *, data=None, headers=None):
        if payload is not None:
            data = json.dumps(payload).encode()
        request_headers = {"Content-Type": "application/json"} if payload is not None else {}
        if data is not None:
            request_headers["X-CSRF-Token"] = next(c.value for c in jar if c.name == "ft_csrf")
        request_headers.update(headers or {})
        req = urllib.request.Request(args.url + path, data=data, headers=request_headers)
        try:
            response = client.open(req, timeout=15)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        return response.status, response.read()

    assert request("/healthz")[0] == 200
    assert request("/login")[0] == 200
    password = "Lab-" + hashlib.sha256(env["FT_ADMIN_PASSWORD"].encode()).hexdigest()[:24]
    receipt_path = root / ".acceptance.json"
    if not receipt_path.exists():
        assert request("/api/auth/login", {"login": "admin", "password": "admin"})[0] == 401
        assert request("/api/auth/login", {"login": "admin", "password": env["FT_ADMIN_PASSWORD"]})[0] == 200
        assert request("/api/auth/change-password", {"current": env["FT_ADMIN_PASSWORD"], "password": password})[0] == 200
        assert request("/api/auth/login", {"login": "admin", "password": password})[0] == 200
        content = bytes(range(256)) * 4096
        status, data = request("/api/files?filename=installer-test.bin&expires_hours=24&is_public=true", data=content,
                               headers={"Content-Type": "application/octet-stream", "X-File-Name": "installer-test.bin"})
        assert status == 201, (status, data)
        item = json.loads(data)["file"]
        receipt = {"env": hashlib.sha256((root / ".env").read_bytes()).hexdigest(),
                   "url": item["download_url"], "content": hashlib.sha256(content).hexdigest()}
        receipt_path.write_text(json.dumps(receipt))
    else:
        receipt = json.loads(receipt_path.read_text())
        assert receipt["env"] == hashlib.sha256((root / ".env").read_bytes()).hexdigest()
        assert request("/api/auth/login", {"login": "admin", "password": password})[0] == 200
    receipt = json.loads(receipt_path.read_text())
    status, content = request(receipt["url"])
    assert status == 200 and hashlib.sha256(content).hexdigest() == receipt["content"]
    status, data = request("/admin/api/settings")
    assert status == 200, (status, data)
    values = json.loads(data)["settings"]
    enabled = args.edition == "bundled" or args.with_downloader
    assert values["offline.enabled"] == enabled
    assert values["site.base_url"] == env["FT_INITIAL_SITE_URL"]
    if enabled:
        for _ in range(30):
            status, data = request("/admin/api/offline/status")
            value = json.loads(data)
            if status == 200 and value.get("service", {}).get("available"):
                break
            time.sleep(2)
        assert status == 200, (status, data)
        assert value.get("service", {}).get("available"), data
        print("Downloader connection and network protection PASS", flush=True)
        if not receipt.get("offline_task"):
            status, data = request("/api/offline/tasks", {"urls": ["https://www.baidu.com/robots.txt"]})
            assert status == 201, (status, data)
            receipt["offline_task"] = json.loads(data)["task"]["id"]
            receipt_path.write_text(json.dumps(receipt))
        for _ in range(120):
            status, data = request("/api/offline/tasks/" + receipt["offline_task"])
            task = json.loads(data)["task"]
            assert task["state"] != "error", task
            if task.get("imported_at"):
                break
            time.sleep(2)
        assert task.get("imported_at"), task
        assert task["is_public"] is False
        print("Real HTTP offline download and private import PASS", flush=True)
    print(args.edition, "random credentials / login / password change / upload / download / defaults / persistence PASS", flush=True)


if __name__ == "__main__":
    main()
