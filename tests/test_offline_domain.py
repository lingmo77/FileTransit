"""Domain normalization, refresh failures and atomic network protection."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.services import offline, offline_network, settings_service

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("value", "expected"), [
    ("https://Pan.Example.com.:8443/path", "pan.example.com"),
    ("https://例子.测试", "xn--fsqu00a.xn--0zwm56d"),
    ("http://8.8.8.8:8000", "8.8.8.8"), ("https://[2606:4700::1111]", "2606:4700::1111"),
    ("", ""), ("pan.example.com", ""), ("ftp://example.com", ""),
    ("https://user:pass@example.com", ""), ("https://a..example.com", ""),
    ("https://example.com:invalid", ""), ("https://[fe80::1%eth0]", ""),
    ("https://exam\nple.com", ""),
    ("https://pan.example.com/a%20b", "pan.example.com"), ("https://@example.com", ""),
])
def test_configured_site_hostname(value, expected):
    assert offline_network.site_hostname(value) == expected


def test_site_host_matching_has_dns_label_boundaries():
    assert offline_network.is_site_url("https://sub.PAN.example.com./x", "pan.example.com")
    assert offline_network.is_site_url("https://pan.example.com:8443/x", "pan.example.com")
    assert not offline_network.is_site_url("https://otherpan.example.com/x", "pan.example.com")
    assert not offline_network.is_site_url("https://pan.example.com.evil.test/x", "pan.example.com")


async def test_create_link_rejects_site_before_dns_or_task_creation(monkeypatch):
    async def enabled(_db):
        pass
    async def get_str(*_args):
        return "https://pan.example.com"
    async def unexpected(_value):
        pytest.fail("a self-site request must be rejected before DNS validation")
    monkeypatch.setattr(offline, "ensure_enabled", enabled)
    monkeypatch.setattr(settings_service, "get_str", get_str)
    monkeypatch.setattr(offline, "validate_http_url", unexpected)
    with pytest.raises(offline.OfflineError, match="本站域名"):
        await offline.create_link_task(None, None, "https://PAN.example.com.:8443/secret")


@pytest.fixture
def guard(tmp_path, monkeypatch):
    control, state = tmp_path / "control", tmp_path / "state"
    control.mkdir()
    state.mkdir()
    monkeypatch.setitem(sys.modules, "runtime", SimpleNamespace(CONTROL=control, STATE=state))
    spec = importlib.util.spec_from_file_location("test_network_guard", ROOT / "docker/aria2/network_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = [1000.0]
    calls = []
    answers = [["8.8.8.8", "2606:4700::1111"]]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module, "update_rules", lambda addresses, ready: calls.append((list(addresses), ready)))
    def resolve(_host):
        value = answers[0]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(module, "resolve", resolve)
    return SimpleNamespace(module=module, control=control, state=state, clock=clock, calls=calls, answers=answers)


def policy(guard, host):
    (guard.control / "policy.json").write_text(json.dumps({"site_hostname": host}))


def test_guard_starts_closed_then_resolves_both_families(guard):
    engine = guard.module.Guard()
    assert not engine.step()
    assert guard.calls[-1] == ([], False)
    policy(guard, "pan.example.com")
    assert engine.step()
    assert guard.calls[-2] == ([], False)
    assert set(guard.calls[-1][0]) == {"8.8.8.8", "2606:4700::1111"}
    result = json.loads((guard.control / "network-status.json").read_text())
    assert result["site_hostname"] == "pan.example.com" and result["ready"]


def test_guard_refresh_retains_old_ips_and_restart_history(guard):
    policy(guard, "pan.example.com")
    engine = guard.module.Guard()
    assert engine.step()
    guard.clock[0] += 21
    guard.answers[0] = ["1.1.1.1"]
    assert engine.step()
    assert set(guard.calls[-1][0]) == {"8.8.8.8", "2606:4700::1111", "1.1.1.1"}
    assert set(guard.module.Guard().history) == set(engine.history)
    guard.clock[0] += guard.module.HISTORY_SECONDS + 1
    assert engine.step()
    assert guard.calls[-1][0] == ["1.1.1.1"]


def test_guard_dns_failure_and_domain_change_close_downloads(guard):
    policy(guard, "pan.example.com")
    engine = guard.module.Guard()
    assert engine.step()
    guard.answers[0] = subprocess.TimeoutExpired("resolver", 8)
    policy(guard, "other.example.com")
    assert not engine.step()
    assert not guard.calls[-1][1] and guard.calls[-2] == ([], False)
    guard.clock[0] += 21
    guard.answers[0] = ["1.1.1.1"]
    assert engine.step()


def test_guard_atomic_updates_only_validated_addresses(guard, monkeypatch):
    spec = importlib.util.spec_from_file_location("real_network_rules", ROOT / "docker/aria2/network_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    module.update_rules(["8.8.8.8", "2606:4700::1111"], True)
    assert len(calls) == 1 and calls[0][0][0] == ["nft", "-f", "-"]
    script = calls[0][1]["input"]
    assert "flush ruleset" not in script and "10001 timeout 15s" in script
    assert "site_v4 { 8.8.8.8 }" in script and "site_v6 { 2606:4700::1111 }" in script
    for bad in ("8.8.8.8; accept", "fe80::1%eth0; accept"):
        with pytest.raises(ValueError):
            module.update_rules([bad], True)
    assert len(calls) == 1


def test_app_rejects_stale_mismatched_or_unready_guard_status(tmp_path, monkeypatch):
    monkeypatch.setattr(offline_network, "settings", SimpleNamespace(offline_root=tmp_path))
    (tmp_path / "control").mkdir()
    monkeypatch.setattr(offline_network.time, "time", lambda: 1000)
    path = tmp_path / "control/network-status.json"
    value = {"site_hostname": "pan.example.com", "ready": True, "updated_at": 999, "expires_at": 1014}
    path.write_text(json.dumps(value))
    assert offline_network.protection_error("pan.example.com") is None
    for changes in ({"updated_at": 900}, {"site_hostname": "old.example.com"}, {"ready": False}, {"expires_at": 999}):
        path.write_text(json.dumps({**value, **changes}))
        assert offline_network.protection_error("pan.example.com")
    assert "填写" in offline_network.protection_error("")
