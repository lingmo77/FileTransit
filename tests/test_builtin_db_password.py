"""内置 PostgreSQL 的密码：数据库容器首次启动时生成并写进 .env，网盘容器从同一个 .env 读取。

直接运行两个容器的启动脚本，用 PATH 里的替身代替 chown、setpriv 和官方 docker-entrypoint.sh。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                                reason="需要 POSIX sh，在测试服务器上运行")

ENV_TEMPLATE = (ROOT / "docker" / "env.example").read_text(encoding="utf-8")


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "chown": "exit 0",
        # 官方镜像的启动脚本：只输出拿到的密码
        "docker-entrypoint.sh": 'echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"',
        # 网盘降权后重新执行入口脚本；这里直接输出网盘拿到的密码
        "setpriv": 'echo "DB_PASSWORD=${DB_PASSWORD:-}"',
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _run_db(tmp_path, stubs, env_file):
    pgdata = tmp_path / "pg" / "18" / "docker"
    pgdata.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "PGDATA": str(pgdata),
           "FT_ENV_FILE": str(env_file)}
    return subprocess.run(["sh", str(ROOT / "docker/postgres/entrypoint.sh"), "postgres"],
                          env=env, capture_output=True, text=True, check=False)


def _password(env_file):
    return re.findall(r"^DB_PASSWORD=(.*)$", env_file.read_text(encoding="utf-8"), re.M)


def test_first_start_writes_password_into_env_in_place(tmp_path, stubs):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
    inode = env_file.stat().st_ino
    result = _run_db(tmp_path, stubs, env_file)
    assert result.returncode == 0, result.stderr
    [password] = _password(env_file)
    assert re.fullmatch(r"[A-Za-z0-9]{40}", password)
    assert f"POSTGRES_PASSWORD={password}" in result.stdout
    # 单文件挂载只能原地改写；其他配置原样保留；只给属主读写
    assert env_file.stat().st_ino == inode
    assert env_file.read_text(encoding="utf-8") == ENV_TEMPLATE.replace(
        "DB_PASSWORD=\n", f"DB_PASSWORD={password}\n")
    assert env_file.stat().st_mode & 0o777 == 0o600

    # 再次启动沿用 .env 里的密码，不重新生成
    (tmp_path / "pg/18/docker/PG_VERSION").write_text("18\n")
    again = _run_db(tmp_path, stubs, env_file)
    assert again.returncode == 0, again.stderr
    assert _password(env_file) == [password]
    assert f"POSTGRES_PASSWORD={password}" in again.stdout


def test_env_without_password_line_gets_one_appended(tmp_path, stubs):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"FT_PORT=8000\r\nDB_BUILTIN=true")  # Windows 换行，末尾没有换行
    assert _run_db(tmp_path, stubs, env_file).returncode == 0
    lines = env_file.read_bytes().decode("utf-8").split("\n")
    assert lines[:2] == ["FT_PORT=8000\r", "DB_BUILTIN=true"]
    assert re.fullmatch(r"DB_PASSWORD=[A-Za-z0-9]{40}", lines[2]) and lines[3:] == [""]


def test_existing_database_without_password_refuses_to_start(tmp_path, stubs):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
    pgdata = tmp_path / "pg" / "18" / "docker"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n")
    result = _run_db(tmp_path, stubs, env_file)
    assert result.returncode == 1
    assert "DB_PASSWORD" in result.stderr and "data/secrets/postgres-password" in result.stderr
    assert _password(env_file) == [""]


def test_missing_env_file_refuses_to_start(tmp_path, stubs):
    (tmp_path / ".env").mkdir()  # docker 挂载不存在的文件时会建成目录
    result = _run_db(tmp_path, stubs, tmp_path / ".env")
    assert result.returncode == 1 and "没有找到 .env" in result.stderr


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="网盘入口脚本以 root 启动")
@pytest.mark.parametrize(("line", "expected"), [
    ("DB_PASSWORD=Abc123\n", "Abc123"),
    ('DB_PASSWORD="Abc123"\r\n', "Abc123"),
    ("DB_PASSWORD=Abc123   # 自动生成\n", "Abc123"),
    ("DB_PASSWORD=\n", ""),
])
def test_app_entrypoint_reads_password_from_mounted_env(tmp_path, stubs, line, expected):
    assert f"DB_PASSWORD={expected}\n" in _run_app(tmp_path, stubs, line, "")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="网盘入口脚本以 root 启动")
def test_app_entrypoint_keeps_password_passed_by_compose(tmp_path, stubs):
    """外置数据库的密码由 compose 的 env_file 传入，以 compose 的解析为准，不用脚本再读一遍。"""
    assert "DB_PASSWORD=a b#c\n" in _run_app(tmp_path, stubs, 'DB_PASSWORD="a b#c"\n', "a b#c")


def _run_app(tmp_path, stubs, line, passed):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DB_BUILTIN=true        # 内置数据库\n{line}", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "FT_ENV_FILE": str(env_file),
           "FT_DATA_DIR": str(data), "DB_PASSWORD": passed}
    env.pop("FT_OFFLINE_DIR", None)
    result = subprocess.run(["sh", str(ROOT / "docker/app/entrypoint.sh")], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout
