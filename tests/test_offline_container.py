"""Downloader safeguards tested without requiring local Docker or a live RPC."""
from __future__ import annotations

import importlib.util
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


def test_offline_package_contains_both_images_and_downloader_sources(tmp_path, monkeypatch):
    package = load_module("offline_package", ROOT / "package.py")
    (tmp_path / "docker/aria2").mkdir(parents=True)
    (tmp_path / "docker/aria2/Dockerfile").write_text("FROM test\n")
    (tmp_path / "docker/aria2/__pycache__").mkdir()
    (tmp_path / "docker/aria2/__pycache__/bad.pyc").write_bytes(b"ignored")
    (tmp_path / "OFFLINE_DOWNLOAD_PLAN.md").write_text("plan")
    (tmp_path / "docker-compose.yml").write_text("services:\n  app: {}\n")
    (tmp_path / "docker-compose.aria2.yml").write_text("services:\n  app: {}\n  aria2: {}\n")
    main = tmp_path / "main.tar"
    engine = tmp_path / "engine.tar"
    main.write_bytes(b"main image")
    engine.write_bytes(b"downloader image")
    monkeypatch.setattr(package, "ROOT", tmp_path)
    monkeypatch.setattr(package, "MANIFEST", ["docker", "OFFLINE_DOWNLOAD_PLAN.md", "docker-compose.yml", "docker-compose.aria2.yml"])
    output = package.build("test-offline", main, engine)
    assert [path.name for path in output] == [
        "file-transfer-src-test-offline.tar.gz", "file-transfer-standard-test-offline.tar.gz",
        "file-transfer-with-aria2-test-offline.tar.gz"]
    with tarfile.open(output[1]) as standard:
        assert "file-transfer/images/file-transfer-1.0.tar" in standard.getnames()
        assert "file-transfer/images/file-transfer-aria2-1.37.0.tar" not in standard.getnames()
        assert package.checksum(engine) not in standard.extractfile("file-transfer/images/SHA256SUMS").read().decode()
        assert b'aria2:' not in standard.extractfile("file-transfer/docker-compose.yml").read()
    with tarfile.open(output[2]) as archive:
        names = archive.getnames()
        assert "file-transfer/docker/aria2/Dockerfile" in names
        assert not any("__pycache__" in name for name in names)
        sums = archive.extractfile("file-transfer/images/SHA256SUMS").read().decode()
        assert package.checksum(main) in sums and package.checksum(engine) in sums
        assert "file-transfer/images/file-transfer-aria2-1.37.0.tar" in names
        assert b'aria2:' in archive.extractfile("file-transfer/docker-compose.yml").read()


def test_package_rejects_downloader_only_archive(tmp_path):
    package = load_module("offline_package", ROOT / "package.py")
    with pytest.raises(ValueError, match="requires"):
        package.build("test", aria2_image=tmp_path / "engine.tar")
