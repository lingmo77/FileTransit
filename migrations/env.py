"""Alembic 环境配置。

连接串统一从应用配置读取（``DB_BUILTIN`` / ``DB_HOST`` 等指向哪里，迁移就作用在哪里），
避免 alembic.ini 与应用配置两处不一致。迁移工具可以通过 ``config.attributes["connection"]``
直接传入连接，升级另一个库。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings.ensure_dirs()
config.set_main_option("sqlalchemy.url", settings.sync_db_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 需要 batch 模式才能 ALTER 表；PostgreSQL 下 batch 直接生成原生 ALTER
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 调用方（迁移工具）可以直接给一个连接，升级配置以外的库
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _migrate(supplied)
        return
    section = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _migrate(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
