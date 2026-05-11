"""Tests for the auto-deploy-default-apps hook."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core import default_apps as da
from compute_space.core.app_id import new_app_id
from compute_space.db.migrations import _schema_path


def _make_cfg(tmp_path: Path, *, apps_dir: Path, default_apps: list[str]) -> DefaultConfig:
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        apps_dir_override=str(apps_dir),
        zone_domain="testzone.local",
        tls_enabled=False,
        start_caddy=False,
        default_apps=default_apps,
    )
    cfg.make_all_dirs()
    return cfg


def _seed_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with open(_schema_path()) as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def _make_app_dir(apps_dir: Path, dir_name: str, *, manifest_name: str) -> None:
    app_dir = apps_dir / dir_name
    app_dir.mkdir(parents=True)
    (app_dir / "openhost.toml").write_text(
        f'[app]\nname = "{manifest_name}"\nversion = "0.1"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    (app_dir / "Dockerfile").write_text("FROM alpine\n")


@pytest.fixture
def cfg_with_apps(tmp_path: Path):
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    _make_app_dir(apps_dir, "secrets_v2", manifest_name="secrets-v2")
    _make_app_dir(apps_dir, "file_browser", manifest_name="file-browser")
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=["secrets_v2", "file_browser"])
    _seed_db(cfg.db_path)
    return cfg


def _patch_insert_and_deploy(monkeypatch, *, fail_for: set[str] | None = None):
    fail_for = fail_for or set()

    def fake(manifest, repo_path, config, db, **kwargs):
        if manifest.name in fail_for:
            raise RuntimeError(f"simulated failure for {manifest.name}")
        next_port = db.execute("SELECT COALESCE(MAX(local_port), 18999) + 1 FROM apps").fetchone()[0]
        app_id = new_app_id()
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, ?, ?, ?, 'building')",
            (app_id, manifest.name, manifest.version or "0.1", repo_path, next_port),
        )
        db.commit()
        return app_id

    monkeypatch.setattr(da, "insert_and_deploy", fake)


def test_deploy_default_apps_installs_each(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch)
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert sum(1 for o in result if o.status == "ok") == 2
    assert sum(1 for o in result if o.status == "failed") == 0
    with open(cfg_with_apps.default_apps_sentinel_path) as f:
        sentinel = json.load(f)
    assert all(entry["status"] == "ok" for entry in sentinel.values())


def test_redeploy_short_circuits_on_terminal_sentinel(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch)
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)

        def must_not_run(*args, **kwargs):
            raise AssertionError("re-walked apps_dir on terminal-sentinel app")

        monkeypatch.setattr(da, "_install_one", must_not_run)

        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert all(o.status == "ok" for o in result)


def test_retries_until_max_attempts(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2", "file-browser"})

    for i in range(da.MAX_RETRY_ATTEMPTS):
        db = sqlite3.connect(cfg_with_apps.db_path)
        try:
            result = da.deploy_default_apps(cfg_with_apps, db)
        finally:
            db.close()
        for o in result:
            assert o.status == "failed"
            assert o.attempts == i + 1

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result_after = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    for o in result_after:
        assert o.attempts == da.MAX_RETRY_ATTEMPTS


def test_malformed_sentinel_is_ignored(cfg_with_apps, monkeypatch):
    """Non-dict sentinel entries (or non-UTF-8 / non-JSON content) must
    not raise — they're treated as if the sentinel were absent."""
    _patch_insert_and_deploy(monkeypatch)
    os.makedirs(os.path.dirname(cfg_with_apps.default_apps_sentinel_path), exist_ok=True)
    with open(cfg_with_apps.default_apps_sentinel_path, "w") as f:
        json.dump({"secrets_v2": "not-a-dict", "file_browser": {"status": "failed"}}, f)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    by_name = {o.name: o.status for o in result}
    assert by_name["secrets_v2"] == "ok"


def test_empty_default_apps_is_no_op(tmp_path: Path):
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=[])
    _seed_db(cfg.db_path)

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert result == []
    assert not os.path.isfile(cfg.default_apps_sentinel_path)
