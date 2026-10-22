"""Bounded v1 torrent parsing. Hash the original info bytes, never re-encode them."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath


class TorrentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentFile:
    index: int
    relative_path: str
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class TorrentMetadata:
    name: str
    info_hash: str
    files: list[TorrentFile]


def safe_relative_path(value: str) -> str:
    if not value or len(value) > 4096 or "\\" in value or value.startswith("/"):
        raise TorrentError("种子包含不安全的文件路径")
    parts = value.split("/")
    if any(not p or p in {".", ".."} or len(p) > 255 or ":" in p
           or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts):
        raise TorrentError("种子包含不安全的文件路径")
    if PurePosixPath(value).is_absolute():
        raise TorrentError("种子包含不安全的文件路径")
    return value


def _text(value) -> str:
    if not isinstance(value, bytes):
        raise TorrentError("种子文件名不合法")
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace")


def parse_torrent(data: bytes, *, max_bytes: int = 5 * 1024**2, max_files: int = 1000) -> TorrentMetadata:
    if not data or len(data) > max_bytes:
        raise TorrentError("种子为空或超过允许的大小")
    offset = 0
    nodes = 0
    info_slice = None

    def parse(depth=0):
        nonlocal offset, nodes, info_slice
        nodes += 1
        if depth > 32 or nodes > 200_000 or offset >= len(data):
            raise TorrentError("种子结构过深、过大或不完整")
        mark = data[offset:offset + 1]
        if mark == b"i":
            end = data.find(b"e", offset + 1)
            if end < 0 or end - offset > 22:
                raise TorrentError("种子整数不合法")
            raw = data[offset + 1:end]
            digits = raw[1:] if raw.startswith(b"-") else raw
            if not digits.isdigit() or raw == b"-0" or (raw.startswith(b"0") and len(raw) > 1) or raw.startswith(b"-0"):
                raise TorrentError("种子整数不合法")
            try:
                number = int(raw)
            except ValueError as exc:
                raise TorrentError("种子整数不合法") from exc
            if not -(2**63) <= number < 2**63:
                raise TorrentError("种子整数超出范围")
            offset = end + 1
            return number
        if mark in {b"l", b"d"}:
            dictionary = mark == b"d"
            offset += 1
            result = {} if dictionary else []
            while offset < len(data) and data[offset:offset + 1] != b"e":
                if dictionary:
                    key = parse(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise TorrentError("种子包含无效或重复字典键")
                    begin = offset
                    result[key] = parse(depth + 1)
                    if depth == 0 and key == b"info":
                        info_slice = (begin, offset)
                else:
                    result.append(parse(depth + 1))
            if offset >= len(data):
                raise TorrentError("种子内容不完整")
            offset += 1
            return result
        if mark.isdigit():
            colon = data.find(b":", offset, min(offset + 12, len(data)))
            if colon < 0:
                raise TorrentError("种子字符串不合法")
            raw = data[offset:colon]
            if not raw.isdigit() or (len(raw) > 1 and raw.startswith(b"0")):
                raise TorrentError("种子字符串长度不合法")
            size = int(raw)
            start = colon + 1
            if start + size > len(data):
                raise TorrentError("种子内容不完整")
            offset = start + size
            return data[start:offset]
        raise TorrentError("不是有效的 BT 种子")

    meta = parse()
    if offset != len(data) or not isinstance(meta, dict) or info_slice is None:
        raise TorrentError("不是有效的 BT 种子")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise TorrentError("种子缺少 info 字典")
    if b"pieces" not in info:
        if info.get(b"meta version") == 2:
            raise TorrentError("当前 Aria2 不支持纯 BT v2 种子，请使用含 v1 信息的种子")
        raise TorrentError("种子缺少校验分块")
    piece_length = info.get(b"piece length")
    pieces = info[b"pieces"]
    if not isinstance(piece_length, int) or piece_length <= 0 or not isinstance(pieces, bytes) or len(pieces) % 20:
        raise TorrentError("种子校验分块不合法")
    name = safe_relative_path(_text(info.get(b"name.utf-8", info.get(b"name"))))
    if "/" in name:
        raise TorrentError("种子根目录名称不合法")
    entries = info.get(b"files")
    result = []
    if entries is None:
        entries = [{b"length": info.get(b"length"), b"path": []}]
    if not isinstance(entries, list) or not 1 <= len(entries) <= max_files:
        raise TorrentError("种子文件数量超过限制或为空")
    seen = set()
    directories = set()
    total = 0
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise TorrentError("种子文件条目不合法")
        attr = entry.get(b"attr", b"")
        if b"symlink path" in entry or (isinstance(attr, bytes) and b"l" in attr):
            raise TorrentError("不支持包含符号链接的种子")
        length = entry.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise TorrentError("种子文件大小不合法")
        components = entry.get(b"path.utf-8", entry.get(b"path"))
        if not isinstance(components, list):
            raise TorrentError("种子文件路径不合法")
        path = safe_relative_path("/".join([name] + [_text(p) for p in components]))
        if path in seen:
            raise TorrentError("种子包含重复文件路径")
        parts = path.split("/")
        parents = {"/".join(parts[:depth]) for depth in range(1, len(parts))}
        if path in directories or parents & seen:
            raise TorrentError("种子包含冲突的文件与目录路径")
        directories.update(parents)
        seen.add(path)
        total += length
        if total >= 2**63:
            raise TorrentError("种子总大小超出范围")
        result.append(TorrentFile(index, path, path.rsplit("/", 1)[-1], length))
    expected_pieces = (total + piece_length - 1) // piece_length
    if len(pieces) // 20 != expected_pieces:
        raise TorrentError("种子文件大小与校验分块数量不匹配")
    begin, end = info_slice
    return TorrentMetadata(name, hashlib.sha1(data[begin:end]).hexdigest(), result)
