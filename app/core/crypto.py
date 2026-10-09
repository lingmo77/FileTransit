"""对称加解密：用于把 SMTP 密码等敏感配置加密后落库。

实现为标准的 **HKDF-SHA256 派生密钥 + HMAC-SHA256 计数器模式密钥流 + Encrypt-then-MAC**
构造，只依赖标准库（避免为一个功能引入重量级密码学依赖）。

> 适用范围：数据库中少量配置项「静态加密」。它不是通用 AEAD，
> 不要用它加密大批量业务数据。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_NONCE_LEN = 16
_TAG_LEN = 32
_INFO_ENC = b"file-transfer/enc"
_INFO_MAC = b"file-transfer/mac"


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256（extract + expand）。"""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(block)
        counter += 1
    return bytes(okm[:length])


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _keys(secret: str, salt: bytes) -> tuple[bytes, bytes]:
    ikm = secret.encode("utf-8")
    return _hkdf(ikm, salt, _INFO_ENC), _hkdf(ikm, salt, _INFO_MAC)


def encrypt(plaintext: str, secret: str) -> str:
    """返回 urlsafe-base64 字符串。"""
    if not plaintext:
        return ""
    nonce = os.urandom(_NONCE_LEN)
    enc_key, mac_key = _keys(secret, nonce)
    raw = plaintext.encode("utf-8")
    ciphertext = bytes(a ^ b for a, b in zip(raw, _keystream(enc_key, nonce, len(raw))))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")


def decrypt(token: str, secret: str) -> str:
    """解密失败（密钥变更 / 数据损坏）时返回空字符串。"""
    if not token:
        return ""
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, TypeError):
        return ""
    if len(blob) < _NONCE_LEN + _TAG_LEN:
        return ""

    nonce = blob[:_NONCE_LEN]
    tag = blob[-_TAG_LEN:]
    ciphertext = blob[_NONCE_LEN:-_TAG_LEN]

    enc_key, mac_key = _keys(secret, nonce)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return ""

    raw = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, nonce, len(ciphertext))))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
