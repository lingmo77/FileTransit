"""把 SQLite 站点的数据迁移到 PostgreSQL。

先停止网盘（迁移期间不能有写入），并把数据库配置改成要迁入的 PostgreSQL：

  完整版：docker compose stop app
          docker compose run --rm app python -m app.tools.sqlite_to_postgresql
  源码版：.venv/bin/python -m app.tools.sqlite_to_postgresql

目标是当前配置的 PostgreSQL（``DB_BUILTIN=true``，或填写了 ``DB_HOST`` 等外置数据库配置）；源默认是数据目录里的
``app.db``，可以用 ``--sqlite`` 指定。源库只读：先做一份临时副本并升级到当前版本的表结构，
再整体导入。目标库已有数据时（例如应用已经启动过、自动建好了管理员和默认设置），需要加
``--overwrite`` 才会先清空目标库里本应用的表。整个导入在一个事务里完成，中途出错时目标库
保持原样。文件本体和签名密钥都在数据目录里，不受影响。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from sqlalchemy import Integer, String, create_engine, delete, func, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from ..core.bootstrap import run_migrations, wait_for_database
from ..core.config import settings
from ..core.database import WRITE_LOCK_KEY
from ..models import Base, User

BATCH = 1000
#: 日志表里的超长字符串直接截断（旧版 SQLite 不检查列宽）；其他表遇到超长值就报错，
#: 不悄悄改动文件名、路径这类数据
TRUNCATABLE = {"activity_logs"}


class MigrationError(Exception):
    pass


def _snapshot(source: Path, workdir: Path) -> Path:
    """用 SQLite 备份接口复制源库，WAL 里尚未写回的内容也会带上。"""
    copy = workdir / "source.db"
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(copy)) as dst:
        src.backup(dst)
    return copy


def _upgrade_snapshot(copy: Path) -> None:
    """旧版本的库先在副本上升级到当前表结构，导入时两边的列才一致。"""
    engine = create_engine(f"sqlite:///{copy.as_posix()}", poolclass=NullPool)
    try:
        with engine.begin() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()


def _fit(table, row: dict, truncated: dict[str, int]) -> dict:
    for column in table.columns:
        value = row.get(column.name)
        length = getattr(column.type, "length", None)
        if not isinstance(column.type, String) or length is None or not isinstance(value, str):
            continue
        if len(value) <= length:
            continue
        if table.name not in TRUNCATABLE:
            key = ", ".join(f"{pk.name}={row.get(pk.name)!r}" for pk in table.primary_key.columns)
            raise MigrationError(
                f"{table.name}.{column.name}（{key}）长度 {len(value)}，超过列宽 {length}；"
                "请先在 SQLite 里修正这条记录")
        row[column.name] = value[:length]
        name = f"{table.name}.{column.name}"
        truncated[name] = truncated.get(name, 0) + 1
    return row


def _reset_sequences(connection: Connection) -> None:
    """导入时写了显式 id，自增序列要推到最大 id 之后，否则新记录会撞主键。"""
    for table in Base.metadata.sorted_tables:
        columns = list(table.primary_key.columns)
        if len(columns) != 1 or not isinstance(columns[0].type, Integer):
            continue
        quoted = connection.dialect.identifier_preparer.quote
        connection.execute(
            text(f"SELECT setval(pg_get_serial_sequence(:table, :column), "
                 f"COALESCE((SELECT MAX({quoted(columns[0].name)}) FROM {quoted(table.name)}), 0) + 1, false)"),
            {"table": table.name, "column": columns[0].name})


def _copy(source: Connection, target: Connection, overwrite: bool, log) -> dict[str, int]:
    tables = Base.metadata.sorted_tables
    # 与应用的写锁同一把：万一网盘没停，它的写事务会被挡住，不会和导入交错
    target.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK_KEY})
    existing = {table.name: target.scalar(select(func.count()).select_from(table)) for table in tables}
    # 迁移本身会建好匿名上传的系统账号，它不算「已有数据」；源库升级后也有同一个账号，导入前先删掉目标库的
    system_users = target.scalar(select(func.count()).select_from(User).where(User.is_system.is_(True))) or 0
    existing["users"] -= system_users
    occupied = {name: count for name, count in existing.items() if count}
    if system_users and not occupied:
        target.execute(delete(User).where(User.is_system.is_(True)))
    if occupied:
        if not overwrite:
            detail = "，".join(f"{name} {count} 行" for name, count in occupied.items())
            raise MigrationError(
                f"目标库已有数据（{detail}），通常是应用已经在这个库上启动过。"
                "确认用 SQLite 的数据覆盖它，请加 --overwrite 重新运行")
        quote = target.dialect.identifier_preparer.quote
        target.execute(text("TRUNCATE " + ", ".join(quote(table.name) for table in tables)
                            + " RESTART IDENTITY CASCADE"))
        log("已清空目标库原有数据")

    counts: dict[str, int] = {}
    truncated: dict[str, int] = {}
    for table in tables:
        count = 0
        batch: list[dict] = []
        result = source.execution_options(yield_per=BATCH).execute(select(table))
        for row in result.mappings():
            batch.append(_fit(table, dict(row), truncated))
            if len(batch) >= BATCH:
                target.execute(insert(table), batch)
                count += len(batch)
                batch.clear()
        if batch:
            target.execute(insert(table), batch)
            count += len(batch)
        counts[table.name] = count
        log(f"  {table.name}: {count} 行")
    _reset_sequences(target)

    for table in tables:
        actual = target.scalar(select(func.count()).select_from(table))
        if actual != counts[table.name]:
            raise MigrationError(f"{table.name} 导入后应有 {counts[table.name]} 行，实际 {actual} 行")
    for name, count in truncated.items():
        log(f"注意：{name} 有 {count} 条超出列宽，已截断")
    return counts


def migrate(source: Path, *, overwrite: bool = False, log=print) -> dict[str, int]:
    """把 ``source`` 的数据导入当前配置的 PostgreSQL，返回每张表导入的行数。"""
    if settings.is_sqlite:
        raise MigrationError("当前配置是 SQLite。请先在 .env 里设置 DB_BUILTIN=true（内置 PostgreSQL），"
                             "或填写 DB_HOST、DB_NAME、DB_USER、DB_PASSWORD（外置 PostgreSQL），指向要迁入的库")
    if not source.is_file():
        raise MigrationError(f"找不到 SQLite 数据库 {source}")
    with tempfile.TemporaryDirectory(prefix="ft-migrate-") as workdir:
        log(f"读取 {source}")
        copy = _snapshot(source, Path(workdir))
        _upgrade_snapshot(copy)

        wait_for_database(settings.sync_db_url)
        run_migrations()  # 目标库建表或升级到当前版本
        log("导入 PostgreSQL：")
        source_engine = create_engine(f"sqlite:///{copy.as_posix()}", poolclass=NullPool)
        target_engine = create_engine(settings.sync_db_url, poolclass=NullPool)
        try:
            with source_engine.connect() as src, target_engine.begin() as dst:
                return _copy(src, dst, overwrite, log)
        finally:
            source_engine.dispose()
            target_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.sqlite_to_postgresql", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", type=Path, default=None,
                        help=f"源 SQLite 文件，默认 {settings.sqlite_path}")
    parser.add_argument("--overwrite", action="store_true",
                        help="目标库已有数据时先清空本应用的表再导入")
    args = parser.parse_args(argv)
    try:
        counts = migrate(args.sqlite or settings.sqlite_path, overwrite=args.overwrite)
    except MigrationError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    print(f"迁移完成，共 {sum(counts.values())} 行。启动网盘后确认数据无误；"
          "原 SQLite 文件没有改动，可以留作备份。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
