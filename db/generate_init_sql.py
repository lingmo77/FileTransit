"""由 Alembic 迁移生成数据库初始化脚本，保证与应用自动建出的结构完全一致。

    python db/generate_init_sql.py           # 重新生成 db/init-*.sql
    python db/generate_init_sql.py --check   # 只比对，不一致时返回非零

新增迁移后必须重新生成并一起提交；测试会检查两者是否同步。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path(__file__).resolve().parent

POSTGRESQL_HEADER = """\
-- 文件中转站 PostgreSQL 初始化脚本（根据数据库迁移自动生成，请勿手改）
--
-- 用法：先建好空库和账号，再执行本脚本，例如
--   createdb -O filetransfer filetransfer
--   psql "postgresql://filetransfer:密码@127.0.0.1:5432/filetransfer" -v ON_ERROR_STOP=1 -f init-postgresql.sql
-- 然后在 .env 设置 FT_DB_MODE=external 和指向这个库的 FT_DATABASE_URL，再启动应用。
--
-- 只建表结构并写入迁移版本号；默认设置和管理员 admin/admin 由应用首次启动时写入。
-- 不执行本脚本也可以：应用连上空库时会自动完成同样的初始化。
-- 只能用于空库，已有数据的库请直接启动新版应用，由它自动迁移。

"""

SQLITE_HEADER = """\
-- 文件中转站 SQLite 初始化脚本（根据数据库迁移自动生成，请勿手改）
--
-- 用法：sqlite3 data/app.db < init-sqlite.sql
--
-- 只建表结构并写入迁移版本号；默认设置和管理员 admin/admin 由应用首次启动时写入。
-- 不执行本脚本也可以：应用启动时发现没有数据库会自动创建。
-- 只能用于新文件，已有数据的库请直接启动新版应用，由它自动迁移。

"""


def _alembic(args: list[str], database_url: str, data_dir: str) -> str:
    env = {**os.environ, "FT_DB_MODE": "external" if database_url else "sqlite",
           "FT_DATABASE_URL": database_url, "FT_DATA_DIR": data_dir,
           "FT_SECRET_KEY": "init-sql-generator", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise SystemExit(f"alembic {' '.join(args)} 失败：\n{result.stderr}")
    return result.stdout


def _tidy(sql: str) -> str:
    """去掉 SQLAlchemy 生成 DDL 时留下的行尾空格。"""
    return "\n".join(line.rstrip() for line in sql.strip().splitlines())


def postgresql_sql() -> str:
    with tempfile.TemporaryDirectory() as data_dir:
        # 离线模式只需要方言，不会真的连接这个地址
        body = _alembic(["upgrade", "head", "--sql"], "postgresql://localhost/filetransfer", data_dir)
    # 否则 psql 执行时会把 RETURNING 的结果行打印出来
    body = body.replace(" RETURNING alembic_version.version_num;", ";")
    return POSTGRESQL_HEADER + _tidy(body) + "\n"


def sqlite_sql() -> str:
    with tempfile.TemporaryDirectory() as data_dir:
        _alembic(["upgrade", "head"], "", data_dir)
        connection = sqlite3.connect(Path(data_dir) / "app.db")
        try:
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name").fetchall()
            versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        finally:
            connection.close()
    lines = ["BEGIN;", ""]
    lines += [f"{_tidy(sql)};\n" for (sql,) in schema]
    lines += [f"INSERT INTO alembic_version (version_num) VALUES ('{version}');" for (version,) in versions]
    lines += ["", "COMMIT;"]
    return SQLITE_HEADER + "\n".join(lines) + "\n"


TARGETS = {"init-postgresql.sql": postgresql_sql, "init-sqlite.sql": sqlite_sql}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只检查是否与迁移一致")
    args = parser.parse_args()
    stale = []
    for name, render in TARGETS.items():
        path = OUTPUT / name
        text = render()
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not args.check:
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"已更新 {path.relative_to(ROOT)}")
    if args.check and stale:
        print("与迁移不一致，请运行 python db/generate_init_sql.py：" + "、".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
