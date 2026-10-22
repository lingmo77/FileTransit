"""Offline user-facing entry points and backend-rendered defaults."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest


@pytest.fixture
async def enabled_offline():
    from app.core.database import SessionLocal
    from app.services import settings_service

    async with SessionLocal() as db:
        previous = await settings_service.get_bool(db, "offline.enabled", False)
        await settings_service.set_many(db, {"offline.enabled": True})
    try:
        yield
    finally:
        async with SessionLocal() as db:
            await settings_service.set_many(db, {"offline.enabled": previous})


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.by_id[values["id"]] = values
        if tag == "a":
            self.links.append(values.get("href"))


async def test_offline_page_requires_login(api):
    response = await api.get("/offline")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/offline"


async def test_offline_page_has_private_creation_and_own_navigation(api, user, enabled_offline):
    response = await api.get("/offline")
    assert response.status_code == 200
    document = Inputs()
    document.feed(response.text)
    assert "/offline" in document.links
    assert "checked" not in document.by_id["offline-public"]
    assert document.by_id["offline-torrent-file"]["accept"] == ".torrent,application/x-bittorrent"
    assert "14 天" in response.text
    assert "/static/js/offline.js?v=" in response.text


async def test_my_files_only_has_global_offline_navigation(api, user, enabled_offline):
    response = await api.get("/dashboard")
    assert response.status_code == 200
    # Global navigation is shared; the personal-files content has no offline creation UI.
    assert 'href="/offline"' in response.text
    assert 'id="offline-new"' not in response.text
    assert 'id="offline-tasks"' not in response.text
    assert 'id="pick-files"' in response.text


async def test_standard_mode_hides_offline_navigation_and_keeps_file_page(api, user):
    from app.core.database import SessionLocal
    from app.services import settings_service

    async with SessionLocal() as db:
        previous = await settings_service.get_bool(db, "offline.enabled", False)
        await settings_service.set_many(db, {"offline.enabled": False})
    try:
        response = await api.get("/dashboard")
        assert response.status_code == 200
        assert 'href="/offline"' not in response.text
        assert 'id="pick-files"' in response.text
        response = await api.post("/api/offline/tasks", json={"urls": ["https://example.com/test.bin"]})
        assert response.status_code == 503
    finally:
        async with SessionLocal() as db:
            await settings_service.set_many(db, {"offline.enabled": previous})


async def test_admin_page_serves_seeding_and_both_rate_controls(admin):
    response = await admin.get("/admin/settings")
    assert response.status_code == 200
    document = Inputs()
    document.feed(response.text)
    for control in ("offline-download-limit", "offline-upload-limit"):
        assert document.by_id[control]["data-type"] == "float"
        assert document.by_id[control]["min"] == "0"
    assert document.by_id["offline-seed-minutes"]["name"] == "offline.seed_minutes"
    assert document.by_id["offline-seed-ratio"]["name"] == "offline.seed_ratio"
