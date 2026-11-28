"""Startup defaults: seeding never replaces site values, passwords stay out of logs, secret key persists."""
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


def _fresh_settings(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.delenv("FT_SECRET_KEY", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_BUILTIN", raising=False)
    return config, config.Settings(_env_file=None, data_dir=tmp_path)


def test_secret_key_is_generated_once_and_reused(tmp_path, monkeypatch):
    config, first = _fresh_settings(tmp_path, monkeypatch)
    config._resolve_secret_key(first)
    assert len(first.secret_key) >= 48 and first.secret_key != config.LEGACY_SECRET_KEY
    _, second = _fresh_settings(tmp_path, monkeypatch)
    config._resolve_secret_key(second)
    assert second.secret_key == first.secret_key
    assert not list(tmp_path.glob(".secret_key.*.tmp"))


def test_explicit_secret_key_wins(tmp_path, monkeypatch):
    config, cfg = _fresh_settings(tmp_path, monkeypatch)
    cfg.secret_key = "from-environment"
    config._resolve_secret_key(cfg)
    assert cfg.secret_key == "from-environment"
    assert not (tmp_path / ".secret_key").exists()


def test_existing_database_without_key_keeps_legacy_default(tmp_path, monkeypatch):
    config, cfg = _fresh_settings(tmp_path, monkeypatch)
    (tmp_path / "app.db").write_bytes(b"")
    config._resolve_secret_key(cfg)
    assert cfg.secret_key == config.LEGACY_SECRET_KEY
    assert (tmp_path / ".secret_key").read_text(encoding="utf-8") == config.LEGACY_SECRET_KEY
