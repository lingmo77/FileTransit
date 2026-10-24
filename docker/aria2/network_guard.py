"""Resolve the configured site and atomically renew a short-lived egress lease.

This process exposes no listener. Only it retains NET_ADMIN; Aria2 and the disk
watchdog have no capabilities. An expired lease blocks outgoing downloads even
if this process hangs or exits, while incoming RPC replies remain available.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import sys
import time

from runtime import CONTROL, STATE

LEASE_SECONDS = 15
REFRESH_SECONDS = 20
HISTORY_SECONDS = 24 * 60 * 60
MAX_ADDRESSES = 4096


def ip_address(value):
    if "%" in str(value):
        raise ValueError("scoped addresses are not supported")
    return ipaddress.ip_address(value)


def hostname(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("missing site hostname")
    try:
        return str(ip_address(value))
    except ValueError:
        pass
    value = value.rstrip(".").lower()
    if not value or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                        for part in value.split(".")):
        raise ValueError("invalid site hostname")
    return value


def policy_hostname() -> str:
    path = CONTROL / "policy.json"
    with path.open("rb") as source:
        data = source.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("policy too large")
    return hostname(json.loads(data).get("site_hostname"))


def resolve(value: str) -> list[str]:
    # A separate bounded process prevents a stuck system resolver from renewing
    # the kernel lease indefinitely. No shell or user-controlled nft text.
    result = subprocess.run([sys.executable, __file__, "--resolve", value],
                            capture_output=True, text=True, check=True, timeout=8)
    answers = json.loads(result.stdout)
    if not isinstance(answers, list) or not 1 <= len(answers) <= 128:
        raise ValueError("invalid site DNS answers")
    return sorted({str(ip_address(item)) for item in answers})


def update_rules(addresses: list[str], ready: bool) -> None:
    groups = {4: [], 6: []}
    for item in addresses:
        address = ip_address(item)
        groups[address.version].append(str(address))
    lines = ["flush set inet ft_offline site_ready",
             "flush set inet ft_offline site_v4", "flush set inet ft_offline site_v6"]
    for version in (4, 6):
        if groups[version]:
            lines.append(f"add element inet ft_offline site_v{version} {{ {', '.join(groups[version])} }}")
    if ready:
        lines.append(f"add element inet ft_offline site_ready {{ 10001 timeout {LEASE_SECONDS}s }}")
    subprocess.run(["nft", "-f", "-"], input="\n".join(lines) + "\n", text=True,
                   check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_json(path, value) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o640)
    temporary.replace(path)


class Guard:
    def __init__(self):
        self.current = None
        self.next_refresh = 0.0
        self.resolved = False
        self.history = {}
        try:
            path = STATE / "network-addresses.json"
            with path.open("rb") as source:
                data = source.read(512 * 1024 + 1)
            values = json.loads(data) if len(data) <= 512 * 1024 else {}
            if isinstance(values, dict) and len(values) <= MAX_ADDRESSES:
                now = time.time()
                self.history = {str(ip_address(key)): float(until)
                                for key, until in values.items() if now < float(until) <= now + HISTORY_SECONDS}
        except (OSError, ValueError, TypeError):
            self.history = {}

    def step(self) -> bool:
        now = time.time()
        error = None
        try:
            current = policy_hostname()
        except (OSError, ValueError, TypeError, AttributeError):
            current = None
            error = "请先在后台填写有效的站点地址（域名）"
        if current != self.current:
            update_rules([], False)  # Close before resolving a changed domain.
            self.current, self.resolved, self.next_refresh = current, False, 0
        if current and now >= self.next_refresh:
            self.next_refresh = now + REFRESH_SECONDS
            try:
                addresses = resolve(current)
                retained = {key: until for key, until in self.history.items() if until > now}
                retained.update({address: now + HISTORY_SECONDS for address in addresses})
                if len(retained) > MAX_ADDRESSES:
                    raise ValueError("too many protected addresses")
                self.history = retained
                write_json(STATE / "network-addresses.json", self.history)
                self.resolved = True
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                self.resolved = False
        ready = bool(current and self.resolved)
        if current and not ready:
            error = "站点域名解析失败，离线下载已暂停，稍后自动重试"
        update_rules(list(self.history), ready)
        write_json(CONTROL / "network-status.json", {
            "site_hostname": current, "ready": ready, "error": error, "updated_at": time.time(),
            "expires_at": time.time() + LEASE_SECONDS if ready else 0,
        })
        return ready


def main():
    guard = Guard()
    while True:
        try:
            guard.step()
        except Exception:
            # No secrets, domain addresses or RPC payloads in logs. Failure to
            # renew the kernel lease closes downloads even if nft itself fails.
            try:
                update_rules([], False)
                write_json(CONTROL / "network-status.json", {
                    "ready": False, "error": "站点网络保护暂不可用，正在重试",
                    "updated_at": time.time(), "expires_at": 0,
                })
            except Exception:
                pass
        time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--resolve":
        value = hostname(sys.argv[2])
        answers = {str(ip_address(row[4][0]))
                   for row in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)}
        print(json.dumps(sorted(answers)))
    else:
        main()
