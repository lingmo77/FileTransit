"""维护任务：过期文件自动删除、临时文件清理、任务锁互斥。"""

from __future__ import annotations

from datetime import timedelta

from .conftest import expire_task_locks


async def _stored_path(file_id: int) -> str:
    """文件在磁盘上的相对路径。

    整个测试会话共用同一个数据目录，所以只能针对自己的文件断言，不能假设目录是空的。
    """
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import FileItem

    async with SessionLocal() as db:
        row = await db.execute(select(FileItem.stored_path).where(FileItem.id == file_id))
        return str(row.scalar_one())


async def _force_expire(file_id: int) -> None:
    """把文件的到期时间拨到过去，模拟「已经到期」。"""
    from sqlalchemy import update

    from app.core.database import SessionLocal
    from app.models import FileItem, utcnow

    async with SessionLocal() as db:
        await db.execute(
            update(FileItem)
            .where(FileItem.id == file_id)
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()


async def _quota(api) -> dict:
    response = await api.get("/api/me/quota")
    assert response.status_code == 200
    return response.json()


async def test_expired_file_is_deleted_and_quota_released(api, user):
    """需求 3：文件到期即自动删除，占用的配额随之释放。"""
    from app.core.config import settings
    from app.services import cleanup

    response = await api.upload("expiring.bin", b"x" * 4096)
    assert response.status_code == 201
    info = response.json()["file"]

    before = await _quota(api)
    assert before["used_files"] == 1
    assert before["used_bytes"] == 4096
    blob = settings.storage_dir / await _stored_path(info["id"])
    assert blob.is_file(), "上传后磁盘上应该有文件本体"

    await _force_expire(info["id"])

    # 过期后立刻不能再下载（410 而不是 404，便于前端给出明确提示），
    # 但详情页仍在，只是显示「已过期」而不是下载按钮
    assert (await api.get(f"/d/{info['public_id']}")).status_code == 410
    detail = await api.get(f"/f/{info['public_id']}")
    assert detail.status_code == 200
    assert "已过期" in detail.text
    assert "下载文件" not in detail.text

    await expire_task_locks()
    result = await cleanup.run_maintenance()
    assert result["expired_files"] >= 1

    # 清理之后记录也没了
    assert (await api.get(f"/d/{info['public_id']}")).status_code == 404
    assert (await api.get(f"/f/{info['public_id']}")).status_code == 404
    assert not blob.exists(), f"过期文件没有从磁盘删除: {blob}"

    after = await _quota(api)
    assert after["used_files"] == 0
    assert after["used_bytes"] == 0


async def test_unexpired_file_survives_maintenance(api, user):
    from app.services import cleanup

    response = await api.upload("kept.bin", b"keep me")
    info = response.json()["file"]

    await expire_task_locks()
    await cleanup.run_maintenance()

    assert (await api.get(f"/f/{info['public_id']}")).status_code == 200
    assert (await api.get(f"/d/{info['public_id']}")).status_code == 200


async def test_maintenance_cleans_only_stale_temp_files(api, user):
    """上传中断留下的临时分片会被回收，正在进行中的上传不能误删。"""
    import os
    import time

    from app.core.config import settings
    from app.services import cleanup

    stale = settings.tmp_dir / "leftover-upload.part"
    stale.write_bytes(b"partial upload" * 100)
    # 清理逻辑只回收超过 24 小时的分片，这里把修改时间拨回两天前
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(stale, (two_days_ago, two_days_ago))

    in_progress = settings.tmp_dir / "still-uploading.part"
    in_progress.write_bytes(b"chunk")

    await expire_task_locks()
    result = await cleanup.run_maintenance()

    assert result["tmp_files"] >= 1
    assert not stale.exists()
    assert in_progress.exists(), "正在进行中的上传被误删了"

    in_progress.unlink()


async def test_maintenance_lock_prevents_concurrent_runs(api, user):
    """多 worker 下同一时刻只允许一个进程做维护。"""
    from app.services import cleanup

    await expire_task_locks()
    assert await cleanup.run_maintenance()  # 拿到锁
    # 锁未到期前，第二个进程会被直接挡掉
    assert await cleanup.run_maintenance() == {}


# ---------------------------------------------------------------- 长期有效


async def test_forever_file_survives_maintenance_and_keeps_quota(admin):
    """长期有效的文件既不能被清理，重算用量时也不能被漏掉。

    ``sync_usage`` 早先用 ``expires_at > now`` 过滤，SQL 里 NULL 参与比较恒为假，
    于是维护跑完用户用量被清零——等于白送配额。
    """
    from sqlalchemy import select, update

    from app.core.database import SessionLocal
    from app.models import FileItem, utcnow
    from app.services import cleanup, quota

    # 先把可能残留的过期文件清干净，下面的增量才只由本用例的两个文件决定
    await expire_task_locks()
    await cleanup.run_maintenance()

    kept = (await admin.upload("forever-quota.bin", b"k" * 4096, expires_hours=0)).json()["file"]
    doomed = (await admin.upload("doomed.bin", b"d" * 2048)).json()["file"]

    before = (await admin.get("/api/me/quota")).json()

    async with SessionLocal() as db:
        await db.execute(
            update(FileItem)
            .where(FileItem.id == doomed["id"])
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()

    await expire_task_locks()
    result = await cleanup.run_maintenance()
    assert result["expired_files"] >= 1

    # 长期有效的文件仍在
    assert (await admin.get(f"/f/{kept['public_id']}")).status_code == 200
    assert (await admin.get(f"/d/{kept['public_id']}")).status_code == 200

    after = (await admin.get("/api/me/quota")).json()
    assert after["used_files"] == before["used_files"] - 1, "长期有效的文件被漏算了"
    assert after["used_bytes"] == before["used_bytes"] - 2048

    # 直接重算一次，长期有效的那 4096 字节依然计入
    async with SessionLocal() as db:
        row = await db.execute(select(FileItem.user_id).where(FileItem.public_id == kept["public_id"]))
        owner_id = int(row.scalar_one())
        count, total = await quota.sync_usage(db, owner_id)
        await db.commit()
    assert count >= 1
    assert total >= 4096


async def test_expired_private_file_is_cleaned_too(api, user):
    """需求：不公开分享的文件到期同样会被删除（清理不区分可见性）。"""
    from app.core.config import settings
    from app.services import cleanup

    response = await api.upload("private-expiring.bin", b"p" * 512, is_public=False)
    info = response.json()["file"]
    blob = settings.storage_dir / await _stored_path(info["id"])

    await _force_expire(info["id"])
    await expire_task_locks()
    result = await cleanup.run_maintenance()
    assert result["expired_files"] >= 1

    assert (await api.get(f"/f/{info['public_id']}")).status_code == 404
    assert not blob.exists(), f"过期私有文件没有从磁盘删除: {blob}"
