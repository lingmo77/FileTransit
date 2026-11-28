"""Downloader safeguards tested without requiring local Docker or a live RPC."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def firewall():
    return load_module("offline_firewall", ROOT / "docker/aria2/firewall.py")


def test_egress_dns_exception_does_not_expose_loopback_rpc_to_downloader(firewall):
    result = firewall.rules(firewall.nameservers("nameserver 127.0.0.11\n"),
                            firewall.blocked_networks("8.8.8.8/32,2606:4700:4700::1111/128"))
    assert "meta skuid 10002 ip daddr 127.0.0.1 tcp dport 6800 accept" in result
    assert "ip daddr 127.0.0.11 meta l4proto udp ct original proto-dst 53 accept" in result
    assert "127.0.0.0/8" in result and "169.254.0.0/16" in result
    assert "ip6 daddr != 2000::/3 reject" in result
    assert "8.8.8.8" in result and "2606:4700:4700::1111" in result
    assert "flush ruleset" not in result
    assert "chain output" in result and "chain forward" not in result


def test_overlapping_protected_networks_are_collapsed(firewall):
    result = firewall.rules(firewall.nameservers("nameserver 127.0.0.11"),
                            firewall.blocked_networks("192.168.50.123/32,10.5.0.0/16"))
    assert "192.168.0.0/16" in result and "192.168.50.123" not in result
    assert "10.0.0.0/8" in result and "10.5.0.0" not in result


def test_firewall_requires_real_resolver_and_valid_networks(firewall):
    with pytest.raises(RuntimeError):
        firewall.nameservers("# no resolver")
    with pytest.raises(ValueError):
        firewall.blocked_networks("1.2.3.4; accept")


def test_container_starts_with_closed_download_lease_without_manual_ips(firewall, monkeypatch):
    monkeypatch.delenv("FT_ARIA2_BLOCKED_NETWORKS", raising=False)
    calls = []
    monkeypatch.setattr(firewall.Path, "read_text", lambda _: "nameserver 127.0.0.11")
    monkeypatch.setattr(firewall.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs["input"]))
    firewall.install()
    assert "meta skuid != @site_ready reject" in calls[0]
    assert "elements = { 10001" not in calls[0]
    assert calls[0].index("jump site_guard") < calls[0].index("ct state established,related accept")
    assert "ct direction original jump site_guard" in calls[0]


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    calls = []
    replies = {"aria2.tellActive": []}

    def rpc(method, params=None):
        calls.append((method, params))
        return replies.get(method, "OK")

    control = tmp_path / "control"
    control.mkdir()
    (tmp_path / "tasks").mkdir()
    monkeypatch.setitem(sys.modules, "runtime", SimpleNamespace(CONTROL=control, DOWNLOADS=tmp_path, rpc=rpc))
    module = load_module("offline_disk_watchdog", ROOT / "docker/aria2/watchdog.py")
    (control / "network-status.json").write_text(json.dumps({"ready": True, "updated_at": time.time(),
                                                           "expires_at": time.time() + 15}))
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000 * 1024**2))
    return SimpleNamespace(module=module, root=tmp_path, control=control, calls=calls, replies=replies)


def write_policy(watchdog, tasks=None, **values):
    payload = {"temp_max_bytes": 100 * 1024**2, "min_free_bytes": 1024**2, "tasks": tasks or {}}
    payload.update(values)
    (watchdog.control / "policy.json").write_text(json.dumps(payload), encoding="utf-8")


def test_watchdog_pauses_all_without_policy(watchdog):
    assert "valid disk policy" in watchdog.module.inspect_once()
    assert watchdog.calls == [("aria2.forcePauseAll", None)]


def test_watchdog_pauses_unrecognized_and_cancelled_transfers(watchdog):
    task_id = "1" * 32
    write_policy(watchdog, {task_id: {"max_bytes": 1024, "desired_state": "cancelled"}})
    watchdog.replies["aria2.tellActive"] = [{"gid": "g1", "dir": str(watchdog.root / "tasks" / task_id / "payload")},
                                           {"gid": "g2", "dir": "/unexpected"}]
    assert watchdog.module.inspect_once() is None
    assert ("aria2.forcePause", ["g1"]) in watchdog.calls
    assert ("aria2.forcePause", ["g2"]) in watchdog.calls


def test_watchdog_stays_active_without_app_and_honors_disk_reserve(watchdog, monkeypatch):
    write_policy(watchdog)
    monkeypatch.setattr(watchdog.module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    assert "disk reserve" in watchdog.module.inspect_once()
    assert ("aria2.forcePauseAll", None) in watchdog.calls


def test_watchdog_bounds_each_task_by_actual_allocated_bytes(watchdog, monkeypatch):
    task_id = "a" * 32
    write_policy(watchdog, {task_id: {"max_bytes": 1024, "desired_state": "running"}})
    watchdog.replies["aria2.tellActive"] = [{"gid": "g1", "dir": str(watchdog.root / "tasks" / task_id / "payload")}]
    monkeypatch.setattr(watchdog.module, "occupied_bytes", lambda path: 4 * 1024**2)
    assert watchdog.module.inspect_once() == "task temporary budget reached"
    assert ("aria2.forcePause", ["g1"]) in watchdog.calls


def test_watchdog_fails_closed_if_disk_usage_cannot_be_read(watchdog, monkeypatch):
    write_policy(watchdog)

    def unreadable(_path):
        raise PermissionError("unreadable task directory")

    monkeypatch.setattr(watchdog.module, "occupied_bytes", unreadable)
    assert watchdog.module.inspect_once() == "temporary disk usage cannot be verified"
    assert ("aria2.forcePauseAll", None) in watchdog.calls


@pytest.mark.parametrize("network", [None, {"ready": False}, {"ready": True, "updated_at": 1, "expires_at": 2}])
def test_watchdog_pauses_without_current_network_protection(watchdog, network):
    write_policy(watchdog)
    path = watchdog.control / "network-status.json"
    if network is None:
        path.unlink()
    else:
        path.write_text(json.dumps(network))
    assert watchdog.module.inspect_once() == "waiting for site network protection"
    assert ("aria2.forcePauseAll", None) in watchdog.calls


def _fake_repo(tmp_path):
    for name in ("app/main.py", "app/__pycache__/main.pyc", "migrations/env.py",
                 "alembic.ini", "requirements.txt", ".env.example", "data/app.db", "README.md", "LICENSE",
                 "install.sh", "db/init-postgresql.sql", "db/init-sqlite.sql", "db/generate_init_sql.py",
                 "tests/test_x.py", "docker/aria2/Dockerfile", "docker/env.example", "AGENTS.md",
                 "RELEASE_NOTES.md", "docker/postgres/entrypoint.sh"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(name)
    (tmp_path / ".env").write_text("FT_SECRET_KEY=DO-NOT-PUBLISH")
    (tmp_path / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
                                                 encoding="utf-8")


def _image_archive(path, tags):
    """模拟 docker save 的产物：只要 manifest.json 里的 RepoTags。"""
    data = json.dumps([{"RepoTags": tags}]).encode()
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path


FULL_TAGS = ["file-transfer:9.9.9", "file-transfer-aria2:9.9.9", "postgres:18.7-trixie"]


def test_full_package_is_compose_plus_images(tmp_path, monkeypatch):
    package = load_module("release_package", ROOT / "package.py")
    _fake_repo(tmp_path)
    images = _image_archive(tmp_path / "images.tar", FULL_TAGS)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    output = package.build("9.9.9", images, tmp_path / "out")
    # 初始化脚本只在安装包里，版本目录里不再单独放
    assert [path.name for path in output] == ["file-transfer-src-9.9.9.tar.gz", "file-transfer-full-9.9.9.tar.gz"]
    assert sorted(path.name for path in (tmp_path / "out" / "9.9.9").iterdir()) == [
        "SHA256SUMS", "file-transfer-full-9.9.9.tar.gz", "file-transfer-src-9.9.9.tar.gz"]
    with tarfile.open(output[1]) as archive:
        names = set(archive.getnames())
        assert names == {"file-transfer/docker-compose.yml", "file-transfer/.env",
                         "file-transfer/README.md", "file-transfer/LICENSE", "file-transfer/VERSION",
                         "file-transfer/db/init-postgresql.sql", "file-transfer/db/init-sqlite.sql",
                         "file-transfer/docker/postgres/entrypoint.sh",
                         "file-transfer/images-9.9.9.tar"}
        assert archive.extractfile("file-transfer/images-9.9.9.tar").read() == images.read_bytes()
        assert archive.getmember("file-transfer/docker/postgres/entrypoint.sh").mode == 0o755
        compose = archive.extractfile("file-transfer/docker-compose.yml").read().decode()
        # 完整版自带 .env（来自 docker/env.example，不是仓库里的 .env），只给属主读写
        env = archive.extractfile("file-transfer/.env").read().decode()
        env_mode = archive.getmember("file-transfer/.env").mode
    assert env == "docker/env.example" and env_mode == 0o600
    assert "build:" not in compose and "dockerfile" not in compose
    # 本项目的镜像固定为发布版本；内置数据库直接用官方镜像，名字和标签不改
    assert package.compose_images(compose) == ["file-transfer:9.9.9", "postgres:18.7-trixie", "file-transfer-aria2:9.9.9"]
    assert compose.count("pull_policy: never") == 3
    assert "docker load -i images-9.9.9.tar" in compose
    # 仓库里的构建说明去掉，写给用户的说明（切换数据库时删哪几处）保留
    assert "package.py" not in compose and "内置 PostgreSQL" in compose
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(compose)
    services = parsed["services"]
    assert set(services) == {"app", "db", "aria2"}
    assert services["app"]["environment"]["DB_BUILTIN"] == "${DB_BUILTIN:-true}"
    assert services["app"]["env_file"] == ".env"
    assert services["app"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    # 数据都用目录挂载到 compose 文件旁边的 data/ 下，不用命名卷；
    # 内置数据库的密码写在 .env 里：db 可写，网盘只读，不再有单独的密码文件
    assert "volumes" not in parsed
    mounts = [mount for service in services.values() for mount in service["volumes"]]
    env_mounts = sorted(mount for mount in mounts if mount.startswith("./.env:"))
    assert env_mounts == ["./.env:/run/ft-env/.env", "./.env:/run/ft-env/.env:ro"]
    assert "./.env:/run/ft-env/.env" in services["db"]["volumes"]
    script = "./docker/postgres/entrypoint.sh:/usr/local/bin/ft-postgres-entrypoint.sh:ro"
    assert script in services["db"]["volumes"]
    assert services["db"]["entrypoint"] == ["sh", "/usr/local/bin/ft-postgres-entrypoint.sh"]
    assert "pg_isready" in services["db"]["healthcheck"]["test"]
    assert all(mount.startswith("./data/") for mount in mounts if mount not in env_mounts and mount != script)
    assert not any("secrets" in mount for mount in mounts)


def test_full_package_requires_every_compose_image(tmp_path, monkeypatch):
    package = load_module("release_package", ROOT / "package.py")
    _fake_repo(tmp_path)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    images = _image_archive(tmp_path / "images.tar", ["file-transfer:9.9.9", "file-transfer-aria2:9.9.9"])
    with pytest.raises(ValueError, match="缺少 postgres:18.7-trixie"):
        package.build("9.9.9", images, tmp_path / "out")
    (tmp_path / "junk.tar").write_bytes(b"not a tar")
    with pytest.raises(ValueError, match="不是 docker save"):
        package.build("9.9.9", tmp_path / "junk.tar", tmp_path / "out")


def test_source_package_contains_only_runtime_sources(tmp_path, monkeypatch):
    package = load_module("release_package", ROOT / "package.py")
    _fake_repo(tmp_path)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    [output] = package.build("9.9.9", output=tmp_path / "out")
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
    assert {"file-transfer/app/main.py", "file-transfer/migrations/env.py", "file-transfer/alembic.ini",
            "file-transfer/requirements.txt", "file-transfer/.env.example",
            "file-transfer/db/init-postgresql.sql", "file-transfer/db/init-sqlite.sql",
            "file-transfer/README.md", "file-transfer/LICENSE"} <= names
    for excluded in ("install.sh", "tests", "docker", "AGENTS.md", ".env", "docker-compose.yml",
                     "db/generate_init_sql.py", "data", "RELEASE_NOTES.md", "DEVELOPMENT.md"):
        assert not any(name == f"file-transfer/{excluded}" or name.startswith(f"file-transfer/{excluded}/")
                       for name in names), excluded
    assert not any("__pycache__" in name for name in names)


def test_packages_use_lf_line_endings(tmp_path, monkeypatch):
    """Windows 上检出的 CRLF 文本文件打包后要和仓库一样是 LF，二进制文件原样保留。"""
    package = load_module("release_package", ROOT / "package.py")
    _fake_repo(tmp_path)
    (tmp_path / "app/crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "app/static.bin").write_bytes(b"\x00\r\n\x01")
    monkeypatch.setattr(package, "ROOT", tmp_path)
    [output] = package.build("9.9.9", output=tmp_path / "out")
    with tarfile.open(output) as archive:
        assert archive.extractfile("file-transfer/app/crlf.py").read() == b"a = 1\nb = 2\n"
        assert archive.extractfile("file-transfer/app/static.bin").read() == b"\x00\r\n\x01"
        assert archive.getmember("file-transfer/app/crlf.py").mode == 0o644


def test_release_keeps_only_two_newest_versions(tmp_path, monkeypatch):
    package = load_module("release_package", ROOT / "package.py")
    _fake_repo(tmp_path)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    out = tmp_path / "out"
    (out / "notes").mkdir(parents=True)                # 不是版本目录
    (out / "0.1.0").mkdir()                            # 版本号命名但没有发布包，不是 package.py 生成的
    (out / "images.tar").write_bytes(b"x")
    for version in ("1.9.0", "1.2.0", "1.10.0", "1.10.0"):  # 1.10.0 打了两次
        package.build(version, output=out)
    assert sorted(path.name for path in out.iterdir()) == ["0.1.0", "1.10.0", "1.9.0", "images.tar", "notes"]
    assert sorted(path.name for path in (out / "1.10.0").iterdir()) == [
        "SHA256SUMS", "file-transfer-src-1.10.0.tar.gz"]
    sums = (out / "1.9.0" / "SHA256SUMS").read_text(encoding="utf-8")
    assert sums.endswith("  file-transfer-src-1.9.0.tar.gz\n")


def test_package_rejects_missing_image_archive(tmp_path):
    package = load_module("release_package", ROOT / "package.py")
    with pytest.raises(ValueError, match="Missing image archive"):
        package.build("test", tmp_path / "missing.tar")
