"""数据库选择（FT_DB_MODE）、初始化脚本与跨数据库可移植的写法。"""
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

from app.core import config
from app.core.database import SessionLocal
from app.models import User
from app.services import quota
from tests.conftest import create_verified_user

ROOT = Path(__file__).resolve().parent.parent


def _settings(tmp_path, monkeypatch, mode: str = "sqlite", url: str = "", **extra):
    for name in ("FT_DATABASE_URL", "FT_DB_MODE", "FT_BUILTIN_DB_HOST", "FT_BUILTIN_DB_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    return config.Settings(_env_file=None, data_dir=tmp_path, db_mode=mode, database_url=url, **extra)


def test_default_is_sqlite_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FT_DB_MODE", raising=False)
    monkeypatch.delenv("FT_DATABASE_URL", raising=False)
    cfg = config.Settings(_env_file=None, data_dir=tmp_path)
    assert cfg.db_mode_normalized == "sqlite"
    assert cfg.is_sqlite and cfg.db_dialect == "sqlite"
    assert cfg.db_path == tmp_path / "app.db"
    assert cfg.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    assert cfg.sync_db_url == f"sqlite:///{(tmp_path / 'app.db').as_posix()}"


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+asyncpg", "postgresql+psycopg"])
def test_external_postgresql_urls_use_bundled_drivers(tmp_path, monkeypatch, scheme):
    cfg = _settings(tmp_path, monkeypatch, "external", f"{scheme}://ft:p%40ss%25w@db.example:5433/filetransfer")
    assert not cfg.is_sqlite and cfg.db_dialect == "postgresql"
    assert cfg.db_path is None
    assert cfg.db_url == "postgresql+asyncpg://ft:p%40ss%25w@db.example:5433/filetransfer"
    assert cfg.sync_db_url == "postgresql+psycopg://ft:p%40ss%25w@db.example:5433/filetransfer"


def test_builtin_reads_generated_password(tmp_path, monkeypatch):
    secret = tmp_path / "postgres-password"
    secret.write_text("Abc123generated\n", encoding="utf-8")
    cfg = _settings(tmp_path, monkeypatch, " Builtin ", builtin_db_password_file=secret)
    assert not cfg.is_sqlite and cfg.db_path is None
    assert cfg.sync_db_url == "postgresql+psycopg://filetransfer:Abc123generated@db:5432/filetransfer"
    assert cfg.db_url == "postgresql+asyncpg://filetransfer:Abc123generated@db:5432/filetransfer"


def test_builtin_without_password_file_explains_what_to_do(tmp_path, monkeypatch):
    cfg = _settings(tmp_path, monkeypatch, "builtin", builtin_db_password_file=tmp_path / "missing")
    with pytest.raises(ValueError, match="db 服务.*FT_DB_MODE"):
        cfg.sync_db_url


@pytest.mark.parametrize(("mode", "url", "message"), [
    ("mysql", "", "FT_DB_MODE 只能是"),
    ("sqlite", "postgresql://u:p@h/db", "FT_DB_MODE 改为 external"),
    ("builtin", "postgresql://u:p@h/db", "FT_DB_MODE 改为 external"),
    ("external", "", "必须设置 FT_DATABASE_URL"),
    ("external", "mysql://u:p@h/db", "只支持 postgresql"),
    ("external", "sqlite:///x.db", "只支持 postgresql"),
    ("external", "not a url", "格式不正确"),
])
def test_invalid_database_settings_are_rejected(tmp_path, monkeypatch, mode, url, message):
    cfg = _settings(tmp_path, monkeypatch, mode, url)
    with pytest.raises(ValueError, match=message):
        cfg.sync_db_url


def test_postgresql_site_gets_random_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("FT_SECRET_KEY", raising=False)
    cfg = _settings(tmp_path, monkeypatch, "external", "postgresql://ft@localhost/filetransfer")
    config._resolve_secret_key(cfg)
    assert cfg.secret_key != config.LEGACY_SECRET_KEY


def test_site_migrated_from_legacy_sqlite_keeps_legacy_secret(tmp_path, monkeypatch):
    """1.5.0 之前没配密钥的 SQLite 站点迁到 PostgreSQL 后，必须沿用旧密钥才能解开已有数据。"""
    monkeypatch.delenv("FT_SECRET_KEY", raising=False)
    (tmp_path / "app.db").write_bytes(b"")
    cfg = _settings(tmp_path, monkeypatch, "external", "postgresql://ft@localhost/filetransfer")
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
                  FT_DATA_DIR=str(tmp_path), FT_DB_MODE="sqlite", FT_DATABASE_URL="",
                  FT_SECRET_KEY="init-script-test")
    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(tmp_path / "app.db")
    try:
        assert connection.execute("SELECT username, is_admin FROM users").fetchall() == [("admin", 1)]
        assert connection.execute("SELECT count(*) FROM settings").fetchone()[0] > 0
    finally:
        connection.close()
