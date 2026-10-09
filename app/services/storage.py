"""磁盘存储：落盘、寻址、删除、用量统计。

存储路径完全由服务端生成（两级哈希前缀 + UUID），**绝不拼接用户输入**，
从根本上杜绝路径穿越。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from ..core.config import settings


class StorageError(Exception):
    pass


class UploadTooLarge(StorageError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__("文件超过允许的最大体积")


@dataclass(slots=True)
class StoredObject:
    rel_path: str
    size: int
    sha256: str


def make_rel_path() -> str:
    """生成 ``ab/cd/<32位hex>`` 形式的两级分片相对路径。"""
    token = uuid4().hex
    return f"{token[:2]}/{token[2:4]}/{token}"


def resolve(rel_path: str) -> Path:
    """把相对路径安全地解析为绝对路径。"""
    root = settings.storage_dir.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise StorageError("非法的存储路径")
    return candidate


def guess_mime(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def safe_filename(filename: str) -> str:
    """清洗上传文件名，只用于展示与下载响应头。"""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*\x7f')
    name = name.strip(" .")
    if not name:
        name = "unnamed"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        keep = 200 - len(ext) - 1 if dot else 200
        name = (stem[:keep] + dot + ext) if dot else name[:200]
    return name


async def store_upload(upload: UploadFile, *, max_bytes: int) -> StoredObject:
    """把上传流写入磁盘。

    先写临时文件再 ``os.replace`` 原子移动，保证不会出现"半个文件被读取"。
    体积超限时抛 :class:`UploadTooLarge`，临时文件会被清理。
    """
    rel_path = make_rel_path()
    dest = resolve(rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.tmp_dir / f"{uuid4().hex}.part"

    digest = hashlib.sha256()
    size = 0
    handle = await run_in_threadpool(open, tmp, "wb")
    try:
        while True:
            chunk = await upload.read(settings.io_chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLarge(max_bytes)
            digest.update(chunk)
            await run_in_threadpool(handle.write, chunk)
        await run_in_threadpool(handle.flush)
        await run_in_threadpool(os.fsync, handle.fileno())
    except BaseException:
        await run_in_threadpool(handle.close)
        tmp.unlink(missing_ok=True)
        raise
    else:
        await run_in_threadpool(handle.close)

    if size == 0:
        tmp.unlink(missing_ok=True)
        raise StorageError("文件内容为空")

    await run_in_threadpool(os.replace, tmp, dest)
    return StoredObject(rel_path=rel_path, size=size, sha256=digest.hexdigest())


async def delete_object(rel_path: str) -> bool:
    """删除文件本体；顺带清理空目录。返回是否真的删掉了。"""
    try:
        path = resolve(rel_path)
    except StorageError:
        return False

    def _remove() -> bool:
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        # 清理空的上级分片目录，避免目录树无限膨胀
        for parent in (path.parent, path.parent.parent):
            try:
                if parent != settings.storage_dir and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                break
        return existed

    return await run_in_threadpool(_remove)


async def cleanup_tmp_dir(max_age_seconds: int = 24 * 3600) -> int:
    """清理上传中断残留的临时分片。"""
    import time

    now = time.time()
    removed = 0
    for entry in settings.tmp_dir.glob("*.part"):
        try:
            if now - entry.stat().st_mtime > max_age_seconds:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


async def disk_usage() -> tuple[int, int, int]:
    """返回 (总空间, 已用空间, 可用空间)，字节。"""

    def _run() -> tuple[int, int, int]:
        usage = shutil.disk_usage(settings.storage_dir if settings.storage_dir.exists() else settings.data_dir)
        return usage.total, usage.used, usage.free

    return await run_in_threadpool(_run)


async def storage_dir_size() -> int:
    """本站文件占用的磁盘空间（字节）。"""

    def _run() -> int:
        total = 0
        for path in settings.storage_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    return await run_in_threadpool(_run)
