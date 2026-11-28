"""由 Alembic 迁移生成数据库初始化脚本，保证与应用自动建出的结构完全一致。

    python db/generate_init_sql.py           # 重新生成 db/init-*.sql
    python db/generate_init_sql.py --check   # 只比对，不一致时返回非零

新增迁移后必须重新生成并一起提交；测试会检查两者是否同步。
"""
from __future__ import annotations

import argparse
import json
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
-- 用于外置 PostgreSQL：先建好空库和账号，再导入本脚本，例如
--   createuser -P filetransfer
--   createdb -O filetransfer filetransfer
--   psql -h 数据库地址 -U filetransfer -d filetransfer -q -v ON_ERROR_STOP=1 -f db/init-postgresql.sql
-- 然后在 .env 设置 DB_BUILTIN=false 和 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD，再启动应用。
--
-- 内容：全部表结构、迁移版本号、默认站点设置和管理员 admin / admin（首次登录强制修改密码）。
-- 「站点地址」和「离线下载开关」两项由应用首次启动时按部署方式写入。
-- 不导入本脚本也可以：应用连上空库时会自动完成同样的初始化。
-- 只能用于空库，已有数据的库请直接启动新版应用，由它自动迁移。

"""

SQLITE_HEADER = """\
-- 文件中转站 SQLite 初始化脚本（根据数据库迁移自动生成，请勿手改）
--
-- 用法：sqlite3 data/app.db < init-sqlite.sql
--
-- 内容：全部表结构、迁移版本号、默认站点设置和管理员 admin / admin（首次登录强制修改密码）。
-- 不执行本脚本也可以：应用启动时发现没有数据库会自动创建。
-- 只能用于新文件，已有数据的库请直接启动新版应用，由它自动迁移。

"""

#: 管理员 admin / admin 的 bcrypt 哈希使用固定盐，脚本才能重复生成出相同内容（--check 依赖这一点）。
#: 口令本身就是公开的默认值，首次登录强制修改，固定盐不降低安全性。
ADMIN_SALT = b"$2b$12$Ft0InitSqlAdminSalt00u"
#: 这两项的初始值取决于部署方式（FT_INITIAL_SITE_URL、完整版默认开启离线下载），留给应用首次启动时写入
ENV_SEEDED = {"site.base_url", "offline.enabled"}


def _env(data_dir: str, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("FT_", "DB_"))}
    env.update({"DB_BUILTIN": "false", "FT_DATA_DIR": data_dir, "FT_SECRET_KEY": "init-sql-generator",
                "PYTHONIOENCODING": "utf-8", **extra})
    return env


def _alembic(args: list[str], data_dir: str, **extra: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
                            cwd=ROOT, env=_env(data_dir, **extra), capture_output=True, text=True,
                            encoding="utf-8", check=False)
    if result.returncode:
        raise SystemExit(f"alembic {' '.join(args)} 失败：\n{result.stderr}")
    return result.stdout


def _seed(dialect: str) -> list[str]:
    """在子进程里用应用的模型和默认值生成种子数据的 INSERT（不受当前环境变量影响）。"""
    with tempfile.TemporaryDirectory() as data_dir:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--seed", dialect],
                                cwd=ROOT, env=_env(data_dir), capture_output=True, text=True,
                                encoding="utf-8", check=False)
    if result.returncode:
        raise SystemExit(f"生成种子数据失败：\n{result.stderr}")
    return result.stdout.strip().split("\n")


def _print_seed(dialect_name: str) -> None:
    sys.path.insert(0, str(ROOT))
    import bcrypt
    from sqlalchemy import insert, literal_column
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models import Setting, User
    from app.services.settings_service import DEFAULTS

    dialect = postgresql.dialect() if dialect_name == "postgresql" else sqlite.dialect()
    # 时间一律存不带时区的 UTC
    now = literal_column("(now() AT TIME ZONE 'utc')" if dialect_name == "postgresql" else "CURRENT_TIMESTAMP")
    # inline()：不加 RETURNING，psql 导入时不会打印结果行
    statements = [insert(User).inline().values(
        username="admin", email="admin@localhost",
        password_hash=bcrypt.hashpw(b"admin", ADMIN_SALT).decode("ascii"),
        is_admin=True, is_active=True, email_verified=True, must_change_password=True,
        can_permanent=False, is_system=False, used_files=0, used_bytes=0, created_at=now)]
    statements += [insert(Setting).values(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=now)
                   for key, value in DEFAULTS.items() if key not in ENV_SEEDED]
    for statement in statements:
        sql = str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        print(" ".join(sql.split()) + ";")


def _with_seed(sql: str, seed: list[str]) -> str:
    """种子数据放在建表的同一个事务里，最后一个 COMMIT 之前。"""
    head, sep, tail = sql.rpartition("COMMIT;")
    if not sep:
        raise SystemExit("迁移脚本里没有 COMMIT")
    return head + "-- 默认设置与管理员\n" + "\n".join(seed) + "\n\n" + sep + tail


def _tidy(sql: str) -> str:
    """去掉 SQLAlchemy 生成 DDL 时留下的行尾空格。"""
    return "\n".join(line.rstrip() for line in sql.strip().splitlines())


def postgresql_sql() -> str:
    with tempfile.TemporaryDirectory() as data_dir:
        # 离线模式只需要方言，不会真的连接这个地址
        body = _alembic(["upgrade", "head", "--sql"], data_dir,
                        DB_HOST="localhost", DB_NAME="filetransfer", DB_USER="filetransfer")
    # 否则 psql 执行时会把 RETURNING 的结果行打印出来
    body = body.replace(" RETURNING alembic_version.version_num;", ";")
    return POSTGRESQL_HEADER + _with_seed(_tidy(body), _seed("postgresql")) + "\n"


def sqlite_sql() -> str:
    with tempfile.TemporaryDirectory() as data_dir:
        _alembic(["upgrade", "head"], data_dir)
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
    return SQLITE_HEADER + _with_seed("\n".join(lines), _seed("sqlite")) + "\n"


TARGETS = {"init-postgresql.sql": postgresql_sql, "init-sqlite.sql": sqlite_sql}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只检查是否与迁移一致")
    parser.add_argument("--seed", choices=["postgresql", "sqlite"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.seed:
        _print_seed(args.seed)
        return 0
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
