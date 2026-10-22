"""Private, bounded JSON-RPC client. Secrets and remote responses never enter logs."""
from __future__ import annotations

import asyncio
import json
from itertools import count

import httpx

from ..core.config import settings

MAX_RESPONSE_BYTES = 64 * 1024**2


class Aria2Error(Exception):
    def __init__(self, message: str = "下载引擎暂时不可用", *, code: int | None = None):
        super().__init__(message)
        self.code = code


class Aria2ResponseTooLarge(Aria2Error):
    pass


class Aria2RPC:
    def __init__(self, *, transport=None):
        self._transport = transport
        self._ids = count(1)

    def _secret(self) -> str:
        if settings.aria2_rpc_secret:
            return settings.aria2_rpc_secret
        try:
            value = settings.offline_secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise Aria2Error("下载引擎尚未配置") from None
        if not value:
            raise Aria2Error("下载引擎尚未配置")
        return value

    async def call(self, method: str, *params):
        payload = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": f"aria2.{method}", "params": ["token:" + self._secret(), *params]}
        try:
            async with httpx.AsyncClient(timeout=8, transport=self._transport, trust_env=False) as client:
                async with client.stream("POST", settings.aria2_rpc_url, json=payload) as response:
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise Aria2ResponseTooLarge("资源文件列表超过允许大小，请减少资源文件数量")
                        chunks.append(chunk)
            result = json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
            raise Aria2Error() from None
        if not isinstance(result, dict):
            raise Aria2Error("下载引擎响应无效")
        if "error" in result:
            error = result["error"]
            code = error.get("code") if isinstance(error, dict) else None
            # The original engine message may contain signed URLs or tracker credentials.
            raise Aria2Error("下载引擎未能完成操作", code=code)
        if not response.is_success:
            raise Aria2Error()
        if "result" not in result:
            raise Aria2Error("下载引擎响应无效")
        return result["result"]

    async def status(self, gid: str, *, detailed: bool = True):
        fields = [
            "gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed",
            "connections", "followedBy", "following", "belongsTo", "infoHash",
            "seeder", "errorCode", "numSeeders", "verifiedLength", "verifyIntegrityPending",
        ]
        if detailed:
            fields += ["files", "bittorrent"]
        return await self.call("tellStatus", gid, fields)


rpc = Aria2RPC()
