"""密码哈希、会话令牌、CSRF、客户端 IP 与限流。"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque

import bcrypt
from anyio import CapacityLimiter, to_thread
from fastapi import Request

from .config import settings

# ---------------------------------------------------------------- 密码

_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_BYTES = 72  # bcrypt 算法上限


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError("密码 UTF-8 编码不能超过 72 字节")
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


_password_workers = CapacityLimiter(4)


async def hash_password_async(password: str) -> str:
    return await to_thread.run_sync(hash_password, password, limiter=_password_workers)


async def verify_password_async(password: str, password_hash: str) -> bool:
    # 旧哈希保持原有 72 字节验证方式，避免已有长口令无法登录。
    return await to_thread.run_sync(verify_password, password, password_hash, limiter=_password_workers)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """返回错误提示；通过校验则返回 None。"""
    if len(password) < 8:
        return "密码至少需要 8 位"
    if len(password) > 128:
        return "密码过长（最多 128 位）"
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        return "密码 UTF-8 编码不能超过 72 字节（中文等字符占多个字节）"
    if password.isdigit() or password.isalpha():
        return "密码需同时包含字母和数字"
    return None


# ---------------------------------------------------------------- 令牌


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """令牌只存哈希，数据库泄露也无法冒用会话。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------- 请求信息


def client_ip(request: Request) -> str:
    """获取客户端真实 IP。

    仅当 ``FT_TRUST_PROXY=true``（部署在可信反向代理之后）时才信任转发头，
    否则客户端可以伪造 ``X-Forwarded-For`` 污染日志。
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:512]


# ---------------------------------------------------------------- 限流


class RateLimiter:
    """进程内滑动窗口限流。

    多 worker 部署时每个进程各有一份计数，属于「弱限流」——足以挡住
    密码爆破与邮件轰炸，不追求全局精确（那需要 Redis，与单机 SQLite 的定位不符）。
    """

    def __init__(self, max_events: int, window_seconds: float, *, enabled: bool = True,
                 max_keys: int = 10000) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self.enabled = enabled
        self._hits: dict[str, deque[float]] = {}
        self.max_keys = max_keys
        self._sweep_at = 0.0

    def _sweep(self, now: float, *, force: bool = False) -> None:
        if not force and now < self._sweep_at:
            return
        self._sweep_at = now + min(30.0, max(0.1, self.window))
        for key, bucket in list(self._hits.items()):
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if not bucket:
                del self._hits[key]

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = deque()
            self._hits[key] = bucket
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        return bucket

    def check(self, key: str) -> bool:
        """记录一次访问；返回 True 表示允许。"""
        if not self.enabled:
            return True
        now = time.monotonic()
        self._sweep(now)
        if key not in self._hits and len(self._hits) >= self.max_keys:
            return False  # 定期清扫之外直接拒绝，避免满容量时每个新键都全表扫描
        bucket = self._prune(key, now)
        if len(bucket) >= self.max_events:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        self._sweep(now)
        if key not in self._hits:
            return max(1, int(self.window) + 1) if len(self._hits) >= self.max_keys else 0
        bucket = self._prune(key, now)
        if not bucket:
            return 0
        return max(1, int(self.window - (time.monotonic() - bucket[0])) + 1)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


_enabled = settings.rate_limit_enabled

login_limiter = RateLimiter(
    settings.rate_limit_login, settings.rate_limit_login_window, enabled=_enabled
)
register_limiter = RateLimiter(
    settings.rate_limit_register, settings.rate_limit_register_window, enabled=_enabled
)
resend_limiter = RateLimiter(
    settings.rate_limit_resend, settings.rate_limit_resend_window, enabled=_enabled
)
upload_limiter = RateLimiter(
    settings.rate_limit_upload, settings.rate_limit_upload_window, enabled=_enabled
)
