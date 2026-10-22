"""Bounded cross-volume imports and task-scoped cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
from pathlib import Path, PurePosixPath

from ..core.config import settings
from . import storage


class OfflineStorageError(Exception):
    pass


def task_dir(task_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise OfflineStorageError("非法的任务标识")
    root = settings.offline_root.resolve()
    directory = root / "tasks" / task_id
    if not directory.resolve().is_relative_to(root) or directory.is_symlink():
        raise OfflineStorageError("非法的任务存储目录")
    return directory


def payload_dir(task_id: str) -> Path:
    directory = task_dir(task_id) / "payload"
    if directory.is_symlink():
        raise OfflineStorageError("非法的任务存储目录")
    return directory


def ensure_task_dir(task_id: str) -> Path:
    directory = payload_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    if os.name != "nt":
        directory.parent.chmod(0o750)
        directory.chmod(0o750)
    return directory


def relative_engine_path(task_id: str, path: str) -> str:
    base = PurePosixPath(settings.aria2_download_dir) / "tasks" / task_id / "payload"
    candidate = PurePosixPath(path)
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        raise OfflineStorageError("下载引擎返回了非法文件路径") from None
    if not relative.parts or any(p in {".", ".."} or "\\" in p or "\x00" in p for p in relative.parts):
        raise OfflineStorageError("下载引擎返回了非法文件路径")
    return relative.as_posix()


def source_path(task_id: str, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    if not parts or PurePosixPath(relative_path).is_absolute() or any(p in {".", ".."} or "\\" in p or "\x00" in p for p in parts):
        raise OfflineStorageError("非法的下载文件路径")
    root = payload_dir(task_id)
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise OfflineStorageError("下载文件超出任务目录")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise OfflineStorageError("下载文件不能包含符号链接")
        if parent == root:
            break
    return path


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                pass
    return total


def write_control(name: str, value: dict) -> None:
    if name not in {"policy.json", "service.json"}:
        raise ValueError("invalid control document")
    directory = settings.offline_root / "control"
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = directory / name
    temporary = directory / (name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
    os.replace(temporary, path)


def copy_import(task_id: str, index: int, relative_path: str, target: str,
                expected_size: int, stop: threading.Event) -> storage.StoredObject:
    source = source_path(task_id, relative_path)
    destination = storage.resolve(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.tmp_dir / f"offline-import-{task_id}-{index}.part"
    digest = hashlib.sha256()
    size = 0
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != expected_size:
            raise OfflineStorageError("下载文件大小或类型不匹配")
        out_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "rb") as reader, os.fdopen(out_fd, "wb") as writer:
            fd = -1
            while True:
                if stop.is_set():
                    raise OfflineStorageError("保存已中止")
                chunk = reader.read(settings.io_chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size:
                    raise OfflineStorageError("下载文件在保存期间发生变化")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            if size != expected_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OfflineStorageError("下载文件在保存期间发生变化")
        if stop.is_set():
            raise OfflineStorageError("保存已中止")
        os.replace(temporary, destination)
        return storage.StoredObject(target, size, digest.hexdigest())
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def cleanup_task(task_id: str) -> None:
    directory = task_dir(task_id)
    # Resolve and verify the exact task directory before any recursive removal.
    expected_parent = (settings.offline_root / "tasks").resolve()
    if directory.resolve().parent != expected_parent:
        raise OfflineStorageError("非法的任务清理路径")
    if directory.exists():
        shutil.rmtree(directory)
    for part in settings.tmp_dir.glob(f"offline-import-{task_id}-*.part"):
        part.unlink(missing_ok=True)
