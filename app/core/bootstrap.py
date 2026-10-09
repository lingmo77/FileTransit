"""启动引导：执行数据库迁移、写入默认设置、创建默认管理员。

这一步是同步阻塞的，调用方需要放进线程池执行。

多 worker 启动时每个进程都会跑一遍，所以整个过程用文件锁串行化：
SQLite 同一时刻只允许一个写入者，并发跑迁移会直接报 ``database is locked``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ..models import Setting, User
from ..services.settings_service import DEFAULTS
from .config import BASE_DIR, settings
from .security import hash_password

logger = logging.getLogger(__name__)

#: 启动锁：等待其它进程完成迁移的最长时间（秒）
LOCK_TIMEOUT = 180.0


@contextmanager
def _startup_lock(timeout: float = LOCK_TIMEOUT):
    """跨进程互斥，保证同一时间只有一个进程在跑迁移。

    用 ``O_CREAT | O_EXCL`` 建锁文件，Windows / Linux 行为一致，且不引入额外依赖。
    持锁进程崩溃会留下陈旧锁文件，因此超过 ``timeout`` 未更新即视为失效并接管。
    """
    path: Path = settings.data_dir / ".startup.lock"
    deadline = time.monotonic() + timeout
    held = False

    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > timeout
            except OSError:
                stale = False
            if stale:
                logger.warning("发现陈旧的启动锁 %s，强制接管", path.name)
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                # 宁可放行也不要卡住启动：迁移本身是幂等的
                logger.warning("等待启动锁超时，直接继续")
                break
            time.sleep(0.2)

    try:
        yield
    finally:
        if held:
            path.unlink(missing_ok=True)


def run_migrations() -> None:
    """把数据库结构升级到最新版本（全新数据库会直接建好所有表）。"""
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.sync_db_url)
    command.upgrade(config, "head")


def _seed_settings(session: Session) -> None:
    existing = {key for (key,) in session.execute(select(Setting.key)).all()}
    for key, value in DEFAULTS.items():
        if key in existing:
            continue
        session.add(Setting(key=key, value=json.dumps(value, ensure_ascii=False)))
    session.commit()


def _seed_admin(session: Session) -> None:
    has_admin = session.execute(select(User.id).where(User.is_admin.is_(True))).first()
    if has_admin:
        return

    admin = User(
        username=settings.admin_username,
        email=f"{settings.admin_username}@localhost",
        password_hash=hash_password(settings.admin_password),
        is_admin=True,
        is_active=True,
        email_verified=True,
        # 默认口令必须首次登录就改掉
        must_change_password=True,
    )
    session.add(admin)
    session.commit()
    logger.warning(
        "已创建默认管理员 %s / %s —— 请登录后立即修改密码",
        settings.admin_username,
        settings.admin_password,
    )


def bootstrap() -> None:
    settings.ensure_dirs()

    with _startup_lock():
        run_migrations()

        engine = create_engine(settings.sync_db_url, future=True)
        try:
            with Session(engine) as session:
                _seed_settings(session)
                _seed_admin(session)
        finally:
            engine.dispose()
