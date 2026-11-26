"""SQLite → PostgreSQL 迁移工具（app.tools.sqlite_to_postgresql）。

真正的导入只在 PostgreSQL 测试轮里跑：另建一个临时库作为目标，不碰整套测试共用的库。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.bootstrap import run_migrations
from app.core.config import settings
from app.models import ActivityLog, FileItem, Setting, User
from app.tools import sqlite_to_postgresql as tool

postgresql_only = pytest.mark.skipif(settings.is_sqlite, reason="需要 PostgreSQL 测试库")


def _make_source(path):
    engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool)
    try:
        with engine.begin() as connection:
            run_migrations(connection)
        with Session(engine) as session:
            session.add_all([
                User(id=7, username="alice", email="alice@example.com", password_hash="x"),
                User(id=3, username="bob", email="bob@example.com", password_hash="y", is_admin=True),
                FileItem(id=41, public_id="pub41", user_id=7, original_name="报告.pdf",
                         stored_path="ab/cd/41", size=2**40, expires_at=datetime(2030, 1, 2, 3, 4, 5)),
                # 旧版 SQLite 不检查列宽，日志里可能有超长路径
                ActivityLog(id=9, username="alice", action="download", path="/f/" + "x" * 600),
                Setting(key="site.name", value='"迁移测试"'),
            ])
            session.commit()
    finally:
        engine.dispose()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_refuses_sqlite_target(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_mode", "sqlite")
    monkeypatch.setattr(settings, "database_url", "")
    with pytest.raises(tool.MigrationError, match="FT_DB_MODE"):
        tool.migrate(tmp_path / "app.db")


@pytest.fixture
def target_database(monkeypatch):
    """临时 PostgreSQL 库；把应用配置临时指向它，用完删除。"""
    base = make_url(settings.sync_db_url)
    name = f"{base.database}_migrate_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = base.set(database=name)
    monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
    try:
        yield url
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@postgresql_only
def test_migrates_all_rows_and_keeps_source_untouched(tmp_path, target_database):
    source = tmp_path / "app.db"
    _make_source(source)
    before = _digest(source)
    messages: list[str] = []

    counts = tool.migrate(source, log=messages.append)

    assert _digest(source) == before
    assert counts["users"] == 2 and counts["files"] == 1 and counts["activity_logs"] == 1
    assert any("activity_logs.path" in line and "截断" in line for line in messages)
    engine = create_engine(target_database, poolclass=NullPool)
    try:
        with Session(engine) as session:
            item = session.get(FileItem, 41)
            assert (item.user_id, item.original_name, item.size) == (7, "报告.pdf", 2**40)
            assert item.expires_at == datetime(2030, 1, 2, 3, 4, 5)
            assert session.get(User, 3).is_admin is True
            assert len(session.get(ActivityLog, 9).path) == 512
            assert session.get(Setting, "site.name").value == '"迁移测试"'
            # 自增序列已推到最大 id 之后，新记录不会撞主键
            session.add(User(username="carol", email="carol@example.com", password_hash="z"))
            session.commit()
            assert session.scalar(select(func.max(User.id))) == 8
    finally:
        engine.dispose()


@postgresql_only
def test_non_empty_target_needs_overwrite(tmp_path, target_database):
    source = tmp_path / "app.db"
    _make_source(source)
    tool.migrate(source, log=lambda _line: None)

    with pytest.raises(tool.MigrationError, match="--overwrite"):
        tool.migrate(source, log=lambda _line: None)

    counts = tool.migrate(source, overwrite=True, log=lambda _line: None)
    assert counts["users"] == 2


@postgresql_only
def test_over_long_value_outside_logs_aborts_without_changes(tmp_path, target_database):
    source = tmp_path / "app.db"
    _make_source(source)
    engine = create_engine(f"sqlite:///{source.as_posix()}", poolclass=NullPool)
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE files SET original_name = ? WHERE id = 41", ("n" * 300,))
    engine.dispose()

    with pytest.raises(tool.MigrationError, match=r"files\.original_name"):
        tool.migrate(source, log=lambda _line: None)

    target = create_engine(target_database, poolclass=NullPool)
    try:
        with target.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(User.__table__)) == 0
    finally:
        target.dispose()
