"""OS 文件锁使多 worker 共享接收并发和临时空间预算，进程退出自动释放。"""
from contextlib import contextmanager
import os

from ..core.config import settings


class UploadBusy(Exception):
    pass


def _lock(path):
    handle = open(path, "a+b")
    try:
        if not os.fstat(handle.fileno()).st_size:
            handle.write(b"0" * 32)
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


@contextmanager
def receive_slot(user_id: int | None, reserved_bytes: int, *,
                 groups: list[tuple[str, int, str]] | None = None):
    """占一个全站接收名额，外加每组各一个名额。

    ``groups`` 是 ``(锁文件前缀, 名额数, 名额用尽时的提示)``；不传时按用户限制并发
    （``upload_user_concurrency``）。匿名上传按 IP 与「全站匿名」两组限制。
    """
    if groups is None:
        groups = [(f"user-{user_id}", settings.upload_user_concurrency, "同时接收的上传过多，请等待其他文件完成")]
    root = settings.tmp_dir / "receive-locks"
    root.mkdir(exist_ok=True)
    handles = []
    guard = _lock(root / "guard.lock")
    if guard is None:
        raise UploadBusy("上传繁忙，请稍后重试")
    try:
        for prefix, limit, message in groups:
            slot = None
            for i in range(max(0, limit)):
                slot = _lock(root / f"{prefix}-{i}.lock")
                if slot:
                    handles.append(slot)
                    break
            if slot is None:
                raise UploadBusy(message)
        global_slot = None
        occupied = 0
        for i in range(settings.upload_concurrency):
            path = root / f"global-{i}.lock"
            candidate = _lock(path)
            if candidate:
                if global_slot is None:
                    global_slot = candidate
                    handles.append(candidate)
                else:
                    candidate.close()
            else:
                try:
                    with path.open("rb") as reader:
                        reader.seek(1)
                        occupied += int(reader.read(32).strip() or b"0")
                except (OSError, ValueError):
                    occupied += settings.max_upload_mb * 1024**2
        if global_slot is None or occupied + reserved_bytes > settings.upload_temp_max_mb * 1024**2:
            raise UploadBusy("接收资源或临时空间已满，请稍后重试")
        # append 模式先 truncate 再写，预算位于第一个锁字节之后。
        global_slot.seek(0)
        global_slot.truncate()
        global_slot.write(b"0" + str(reserved_bytes).encode().ljust(32))
        global_slot.flush()
    except BaseException:
        for handle in reversed(handles):
            handle.close()
        raise
    finally:
        guard.close()
    try:
        yield
    finally:
        for handle in reversed(handles):
            handle.close()
