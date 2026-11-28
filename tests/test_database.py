"""数据库选择（DB_BUILTIN / DB_HOST 等）、初始化脚本与跨数据库可移植的写法。"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from app.core import config
from app.core.database import SessionLocal
from app.models import User
from app.services import quota
from tests.conftest import create_verified_user

ROOT = Path(__file__).resolve().parent.parent


DB_ENV = ("DB_BUILTIN", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
          "FT_DB_MODE", "FT_DATABASE_URL", "FT_BUILTIN_DB_HOST")


def _clean(monkeypatch):
    for name in DB_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(tmp_path, monkeypatch, **fields):
    _clean(monkeypatch)
    return config.Settings(_env_file=None, data_dir=tmp_path, **fields)


def _from_env_file(tmp_path, monkeypatch, text):
    _clean(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return config.Settings(_env_file=env, data_dir=tmp_path)


EXTERNAL = {"db_host": "db.example", "db_port": "5433", "db_name": "filetransfer", "db_user": "ft"}


def test_default_is_sqlite_in_data_dir(tmp_path, monkeypatch):
    cfg = _settings(tmp_path, monkeypatch)
    assert cfg.db_kind == "sqlite"
    assert cfg.is_sqlite and cfg.db_dialect == "sqlite"
    assert cfg.db_path == tmp_path / "app.db"
    assert cfg.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    assert cfg.sync_db_url == f"sqlite:///{(tmp_path / 'app.db').as_posix()}"


def test_external_postgresql_from_separate_fields(tmp_path, monkeypatch):
    """密码照原样填写，特殊字符由应用转义。"""
    cfg = _settings(tmp_path, monkeypatch, **EXTERNAL, db_password="p@ss%w:/#")
    assert cfg.db_kind == "external"
    assert not cfg.is_sqlite and cfg.db_dialect == "postgresql"
    assert cfg.db_path is None
    assert cfg.db_url == "postgresql+asyncpg://ft:p%40ss%25w%3A%2F%23@db.example:5433/filetransfer"
    assert cfg.sync_db_url == "postgresql+psycopg://ft:p%40ss%25w%3A%2F%23@db.example:5433/filetransfer"


def test_env_file_in_release_format(tmp_path, monkeypatch):
    """与完整版 .env 相同的写法：DB_BUILTIN 行带行尾注释，其余各占一行。"""
    cfg = _from_env_file(tmp_path, monkeypatch, (
        "DB_BUILTIN=false        # true：内置数据库（默认）；false：外置数据库，为空使用sqlite\n"
        "DB_HOST=10.0.0.5\nDB_PORT=5432\nDB_NAME=ftdb\nDB_USER=ft\nDB_PASSWORD=\"a b#c\"\n"))
    assert cfg.db_kind == "external"
    url = make_url(cfg.sync_db_url)
    assert (url.drivername, url.username, url.password, url.host, url.port, url.database) == (
        "postgresql+psycopg", "ft", "a b#c", "10.0.0.5", 5432, "ftdb")


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on"])
def test_builtin_switch_reads_generated_password(tmp_path, monkeypatch, raw):
    """完整版的 .env：DB_BUILTIN=true，密码由数据库容器写在 DB_PASSWORD，其余几项不用填。"""
    cfg = _from_env_file(tmp_path, monkeypatch, (
        f"DB_BUILTIN={raw}        # 注释\nDB_HOST=\nDB_PORT=5432\nDB_NAME=\nDB_USER=\n"
        "DB_PASSWORD=Abc123generated\n"))
    assert cfg.db_kind == "builtin" and not cfg.is_sqlite and cfg.db_path is None
    assert cfg.sync_db_url == "postgresql+psycopg://filetransfer:Abc123generated@db:5432/filetransfer"
    assert cfg.db_url == "postgresql+asyncpg://filetransfer:Abc123generated@db:5432/filetransfer"


@pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
def test_builtin_false_without_host_is_sqlite(tmp_path, monkeypatch, raw):
    cfg = _from_env_file(tmp_path, monkeypatch, f"DB_BUILTIN={raw}\nDB_HOST=\nDB_PASSWORD=leftover\n")
    assert cfg.db_kind == "sqlite"


def test_builtin_without_password_explains_what_to_do(tmp_path, monkeypatch):
    cfg = _settings(tmp_path, monkeypatch, builtin_db=True)
    with pytest.raises(ValueError, match="DB_PASSWORD.*db 服务.*DB_BUILTIN 改为 false"):
        cfg.sync_db_url


@pytest.mark.parametrize("legacy", [{"db_mode": "builtin"}, {"database_url": "postgresql://u:p@h/db"}])
def test_legacy_settings_are_rejected_with_instructions(tmp_path, monkeypatch, legacy):
    """1.5.0 的 FT_DB_MODE / FT_DATABASE_URL 不再生效：照旧值猜测可能连错库，所以直接报错并说明怎么改。"""
    cfg = _settings(tmp_path, monkeypatch, **legacy)
    with pytest.raises(ValueError, match="已不再使用.*DB_BUILTIN"):
        cfg.sync_db_url


@pytest.mark.parametrize(("fields", "message"), [
    ({"builtin_db": True, "db_password": "x", "db_host": "h"}, "DB_BUILTIN 改为 false"),
    ({"db_host": "h", "db_user": "u"}, "必须填写 DB_NAME"),
    ({"db_host": "h", "db_name": "n"}, "必须填写 DB_USER"),
    ({**EXTERNAL, "db_port": "abc"}, "DB_PORT 必须是"),
    ({**EXTERNAL, "db_port": "70000"}, "DB_PORT 必须是"),
])
def test_invalid_database_settings_are_rejected(tmp_path, monkeypatch, fields, message):
    cfg = _settings(tmp_path, monkeypatch, **fields)
    with pytest.raises(ValueError, match=message):
        cfg.sync_db_url


def test_empty_port_defaults_to_5432(tmp_path, monkeypatch):
    cfg = _settings(tmp_path, monkeypatch, **{**EXTERNAL, "db_port": ""})
    assert cfg.sync_db_url.endswith("@db.example:5432/filetransfer")


def test_invalid_builtin_switch_is_rejected(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("DB_BUILTIN", "maybe")
    with pytest.raises(ValueError):
        config.Settings(_env_file=None, data_dir=tmp_path)


def test_postgresql_site_gets_random_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("FT_SECRET_KEY", raising=False)
    cfg = _settings(tmp_path, monkeypatch, **EXTERNAL)
    config._resolve_secret_key(cfg)
    assert cfg.secret_key != config.LEGACY_SECRET_KEY


def test_site_migrated_from_legacy_sqlite_keeps_legacy_secret(tmp_path, monkeypatch):
    """1.5.0 之前没配密钥的 SQLite 站点迁到 PostgreSQL 后，必须沿用旧密钥才能解开已有数据。"""
    monkeypatch.delenv("FT_SECRET_KEY", raising=False)
    (tmp_path / "app.db").write_bytes(b"")
    cfg = _settings(tmp_path, monkeypatch, **EXTERNAL)
    config._resolve_secret_key(cfg)
    assert cfg.secret_key == config.LEGACY_SECRET_KEY


def test_url_encoded_password_survives_alembic_config():
    url = "postgresql+psycopg://ft:p%40ss%25w@localhost/filetransfer"
    alembic = AlembicConfig()
    alembic.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    assert alembic.get_main_option("sqlalchemy.url") == url


def test_release_does_not_use_sqlite_only_max():
    stmt = quota.update(User).values(
        used_files=quota.case((User.used_files > 1, User.used_files - 1), else_=0))
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "max(" not in compiled.lower()


async def test_release_never_goes_below_zero(user):
    async with SessionLocal() as db:
        await quota.release(db, user["id"], 10**12)
        await quota.release(db, user["id"], 10**12)
        await db.commit()
        row = (await db.execute(select(User.used_files, User.used_bytes).where(User.id == user["id"]))).one()
    assert tuple(row) == (0, 0)


async def test_write_transactions_are_serialized_without_deadlock(api):
    """业务代码依赖单写者语义；PostgreSQL 下两个事务反序锁行也不能死锁。"""
    first = await create_verified_user(api, "lock_a_")
    second = await create_verified_user(api, "lock_b_")
    order: list[str] = []

    async def writer(name: str, ids: list[int]) -> None:
        async with SessionLocal() as db:
            await db.execute(update(User).where(User.id == ids[0]).values(used_files=User.used_files))
            order.append(f"{name}-locked")
            await asyncio.sleep(0.3)
            await db.execute(update(User).where(User.id == ids[1]).values(used_files=User.used_files))
            await db.commit()
            order.append(f"{name}-committed")

    await asyncio.gather(writer("a", [first["id"], second["id"]]),
                         writer("b", [second["id"], first["id"]]))
    # 第二个写事务必须等第一个提交后才能开始写，不会交错
    assert order in (["a-locked", "a-committed", "b-locked", "b-committed"],
                     ["b-locked", "b-committed", "a-locked", "a-committed"])


async def test_search_is_case_insensitive(admin, user):
    # SQLite 的 LIKE 本来就不区分 ASCII 大小写，PostgreSQL 上也必须保持一致
    listing = await admin.get("/admin/api/users", params={"q": user["username"].upper()})
    assert listing.status_code == 200, listing.text
    assert [item["username"] for item in listing.json()["items"]] == [user["username"]]


def _run(args, **env):
    full = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=full, capture_output=True,
                          text=True, encoding="utf-8", check=False)


def test_init_scripts_match_migrations():
    result = _run(["db/generate_init_sql.py", "--check"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_starts_on_database_created_from_sqlite_init_script(tmp_path):
    connection = sqlite3.connect(tmp_path / "app.db")
    connection.executescript((ROOT / "db" / "init-sqlite.sql").read_text(encoding="utf-8"))
    connection.close()
    result = _run(["-c", "from app.core.bootstrap import bootstrap; bootstrap()"],
                  FT_DATA_DIR=str(tmp_path), DB_BUILTIN="false", DB_HOST="",
                  FT_SECRET_KEY="init-script-test")
    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(tmp_path / "app.db")
    try:
        assert sorted(connection.execute("SELECT username, is_admin, is_system FROM users").fetchall()) == [
            ("@anonymous", 0, 1), ("admin", 1, 0)]
        assert connection.execute("SELECT count(*) FROM settings").fetchone()[0] > 0
    finally:
        connection.close()


@pytest.mark.skipif(config.settings.is_sqlite, reason="需要 PostgreSQL 测试库")
def test_external_postgresql_initialised_from_init_script(tmp_path):
    """外置数据库的用法：空库导入 init-postgresql.sql，应用直接启动，管理员 admin/admin 可用。"""
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from app.core.security import verify_password

    base = make_url(config.settings.sync_db_url)
    name = f"{base.database}_init_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    target = create_engine(base.set(database=name), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with target.connect() as connection:
            connection.exec_driver_sql((ROOT / "db" / "init-postgresql.sql").read_text(encoding="utf-8"))
        result = _run(["-c", "from app.core.bootstrap import bootstrap; bootstrap()"],
                      FT_DATA_DIR=str(tmp_path), FT_SECRET_KEY="init-script-test", DB_NAME=name)
        assert result.returncode == 0, result.stderr
        with target.connect() as connection:
            users = connection.execute(text(
                "SELECT username, password_hash, is_admin, must_change_password, created_at FROM users"
                " WHERE NOT is_system")).all()
            system = connection.execute(text("SELECT username FROM users WHERE is_system")).scalars().all()
            keys = {key for (key,) in connection.execute(text("SELECT key FROM settings"))}
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        [(username, password_hash, is_admin, must_change, created_at)] = users
        assert (username, is_admin, must_change) == ("admin", True, True)
        assert system == ["@anonymous"]  # 匿名上传的系统账号由迁移写入
        assert verify_password("admin", password_hash) and created_at is not None
        # 脚本没写的两项由应用启动时补上
        assert {"site.name", "quota.max_upload_mb", "site.base_url", "offline.enabled"} <= keys
        assert version == "b5e1d7c3a9f4"
    finally:
        target.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()
