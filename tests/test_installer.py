"""Installer defaults must never replace existing site values or expose passwords."""
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import bootstrap
from app.models import Base, Setting


def test_installer_seeds_only_missing_values(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.settings, "initial_site_url", "http://192.0.2.1:9000")
    monkeypatch.setattr(bootstrap.settings, "initial_offline_enabled", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'install.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            bootstrap._seed_settings(session)
            assert json.loads(session.get(Setting, "site.base_url").value) == "http://192.0.2.1:9000"
            assert json.loads(session.get(Setting, "offline.enabled").value) is True
            session.get(Setting, "site.base_url").value = json.dumps("https://existing.example")
            session.get(Setting, "offline.enabled").value = "false"
            session.commit()
            bootstrap._seed_settings(session)
            assert json.loads(session.get(Setting, "site.base_url").value) == "https://existing.example"
            assert json.loads(session.get(Setting, "offline.enabled").value) is False
    finally:
        engine.dispose()


def test_generated_password_is_not_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(bootstrap.settings, "admin_password", "unique-install-password-do-not-log")
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            bootstrap._seed_admin(session)
        assert "unique-install-password-do-not-log" not in caplog.text
        assert "已创建管理员" in caplog.text
    finally:
        engine.dispose()
