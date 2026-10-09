"""纯函数级别的安全组件测试（限流、密码、CSRF、会话令牌）。"""

from __future__ import annotations

import time

import pytest

from app.core.security import (
    RateLimiter,
    hash_password,
    new_token,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    hashed = hash_password("correct horse battery staple")

    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$2b$")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)

    # 同一密码两次哈希结果不同（随机盐），但都能验证通过
    assert hash_password("same") != hash_password("same")


def test_password_beyond_bcrypt_limit_does_not_crash():
    """bcrypt 只取前 72 字节，超长密码必须被安全截断而不是抛异常。"""
    password = "a" * 200
    hashed = hash_password(password)

    assert verify_password(password, hashed)
    # 前 72 字节相同即视为同一密码（bcrypt 的固有行为，这里固定住以免回归）
    assert verify_password("a" * 72, hashed)


def test_new_token_is_urlsafe_and_unique():
    tokens = {new_token() for _ in range(200)}

    assert len(tokens) == 200
    assert all(len(token) > 20 for token in tokens)
    assert all(all(c.isalnum() or c in "-_" for c in token) for token in tokens)


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(max_events=3, window_seconds=60)

    assert [limiter.check("ip") for _ in range(3)] == [True, True, True]
    assert limiter.check("ip") is False
    # 不同 key 互不影响
    assert limiter.check("other-ip") is True


def test_rate_limiter_window_expires(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    limiter = RateLimiter(max_events=2, window_seconds=60)
    assert limiter.check("ip")
    assert limiter.check("ip")
    assert not limiter.check("ip")

    clock["now"] += 61  # 滑出窗口
    assert limiter.check("ip")


def test_rate_limiter_retry_after_and_reset():
    limiter = RateLimiter(max_events=1, window_seconds=60)
    assert limiter.check("ip")
    assert 1 <= limiter.retry_after("ip") <= 61

    limiter.reset("ip")
    assert limiter.check("ip")
    assert limiter.retry_after("ip") >= 1


def test_rate_limiter_can_be_disabled():
    limiter = RateLimiter(max_events=1, window_seconds=60, enabled=False)

    assert all(limiter.check("ip") for _ in range(50))
    assert limiter.retry_after("ip") == 0


@pytest.mark.parametrize("value", [0, -1, 10**9])
def test_retry_after_never_returns_negative(value):
    limiter = RateLimiter(max_events=5, window_seconds=value)
    limiter.check("ip")

    assert limiter.retry_after("ip") >= 0
