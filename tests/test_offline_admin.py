"""Offline controls must persist valid values without exposing engine credentials."""
from __future__ import annotations

import pytest


async def test_offline_admin_controls_reject_guest_and_normal_user(api, new_client, user):
    guest = await new_client()
    assert (await guest.get("/admin/api/offline/status")).status_code == 401
    assert (await guest.post("/admin/api/settings", json={"offline.enabled": True})).status_code == 401
    assert (await api.get("/admin/api/offline/status")).status_code == 403
    assert (await api.post("/admin/api/settings", json={"offline.enabled": True})).status_code == 403


async def test_admin_offline_seeding_and_zero_rate_limits_are_persistent(admin):
    from app.services import settings_service

    keys = ("offline.seed_enabled", "offline.seed_minutes", "offline.seed_ratio",
            "offline.download_limit_mbps", "offline.upload_limit_mbps")
    original = (await admin.get("/admin/api/settings")).json()["settings"]
    try:
        response = await admin.post("/admin/api/settings", json={
            "offline.seed_enabled": True, "offline.seed_minutes": 90, "offline.seed_ratio": 1.5,
            "offline.download_limit_mbps": 2.5, "offline.upload_limit_mbps": .75,
        })
        assert response.status_code == 200, response.text
        response = await admin.post("/admin/api/settings", json={
            "offline.download_limit_mbps": 0, "offline.upload_limit_mbps": 0,
        })
        assert response.status_code == 200, response.text
        settings_service.invalidate()
        current = (await admin.get("/admin/api/settings")).json()["settings"]
        assert current["offline.seed_enabled"] is True
        assert current["offline.seed_minutes"] == 90
        assert current["offline.seed_ratio"] == 1.5
        assert current["offline.download_limit_mbps"] == 0
        assert current["offline.upload_limit_mbps"] == 0
    finally:
        restored = await admin.post("/admin/api/settings", json={key: original[key] for key in keys})
        assert restored.status_code == 200, restored.text


@pytest.mark.parametrize("key,value", [
    ("offline.download_limit_mbps", -1),
    ("offline.upload_limit_mbps", -.01),
    ("offline.download_limit_mbps", "NaN"),
    ("offline.upload_limit_mbps", "Infinity"),
    ("offline.seed_minutes", 0),
    ("offline.seed_minutes", 1.5),
    ("offline.seed_ratio", -1),
    ("offline.seed_enabled", "false"),
])
async def test_invalid_offline_settings_do_not_partially_save(admin, key, value):
    original = (await admin.get("/admin/api/settings")).json()["settings"]
    enabled = original["offline.enabled"]
    response = await admin.post("/admin/api/settings", json={"offline.enabled": not enabled, key: value})
    assert response.status_code == 400, response.text
    after = (await admin.get("/admin/api/settings")).json()["settings"]
    assert after["offline.enabled"] is enabled
    assert after[key] == original[key]


async def test_nonfinite_json_rate_limit_is_rejected_before_persistence(admin):
    original = (await admin.get("/admin/api/settings")).json()["settings"]["offline.download_limit_mbps"]
    headers = {**admin.headers(), "Content-Type": "application/json"}
    for literal in ("NaN", "Infinity", "-Infinity"):
        response = await admin.client.post(
            "/admin/api/settings", headers=headers,
            content=('{"offline.download_limit_mbps":' + literal + '}').encode(),
        )
        assert response.status_code == 400, response.text
    after = (await admin.get("/admin/api/settings")).json()["settings"]
    assert after["offline.download_limit_mbps"] == original


async def test_offline_status_does_not_expose_rpc_secret_or_connection_url(admin, monkeypatch):
    from app.core.config import settings

    secret = "offline-test-sensitive-token-must-not-escape"
    rpc_url = "http://rpc-user:rpc-password@internal-download-engine:6800/jsonrpc"
    monkeypatch.setattr(settings, "aria2_rpc_secret", secret)
    monkeypatch.setattr(settings, "aria2_rpc_url", rpc_url)
    response = await admin.get("/admin/api/offline/status")
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert rpc_url not in response.text
    assert "rpc-password" not in response.text
    service = response.json()["service"]
    assert isinstance(service["enabled"], bool)
    assert isinstance(service["available"], bool)
    assert not any("secret" in key.lower() or "token" in key.lower() or "rpc_url" in key.lower() for key in service)
