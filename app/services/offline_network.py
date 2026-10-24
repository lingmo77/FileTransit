"""Site hostname normalization and the downloader's private protection status."""
from __future__ import annotations

import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

from ..core.config import settings


def site_hostname(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return ""
        parsed = urlsplit(value)
        _ = parsed.port
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            return ""
        host = (parsed.hostname or "").rstrip(".").lower()
        if "%" in host:
            return ""
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        host = host.encode("idna").decode("ascii")
        if not host or len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
            return ""
        return host
    except (ValueError, UnicodeError, TypeError):
        return ""


def is_site_url(value: str, configured_host: str) -> bool:
    host = site_hostname(value)
    return bool(configured_host and host and
                (host == configured_host or host.endswith("." + configured_host)))


def protection_error(configured_host: str) -> str | None:
    if not configured_host:
        return "请先在后台填写有效的站点地址（域名）"
    try:
        value = json.loads((settings.offline_root / "control" / "network-status.json").read_text(encoding="utf-8"))
        now = time.time()
        if (value.get("site_hostname") != configured_host or not 0 <= now - float(value["updated_at"]) < 15):
            return "站点网络保护正在更新，请稍后重试"
        if value.get("ready") is True and now < float(value["expires_at"]) <= now + 15:
            return None
        return "站点域名解析或网络保护暂不可用，离线下载已暂停，稍后自动重试"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "站点网络保护正在连接，请稍后重试"
