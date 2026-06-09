"""Unit tests for the per-app cert renewal sweep (no network).

Exercises renew_app_certs_once's decision logic: which apps it touches, and
whether it restarts the container based on cert-file mtime changes.  The
provisioning and restart calls are monkeypatched so no podman/ACME is needed.
"""

from __future__ import annotations

import os
import sqlite3

from compute_space.config import DefaultConfig
from compute_space.core import tls
from compute_space.core.tls import renewal
from compute_space.core.tls.app_certs import app_cert_dir


def _config(tmp_path):
    data_root = tmp_path / "data"
    (data_root).mkdir()
    return DefaultConfig(
        zone_domain="alice.example.com",
        data_root_dir=str(data_root),
        tls_enabled=True,
    )


def _make_db(config):
    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE apps (
            app_id TEXT PRIMARY KEY, name TEXT, status TEXT, repo_path TEXT
        )"""
    )
    db.commit()
    db.close()


def _add_app(config, app_id, name, status, repo_path):
    db = sqlite3.connect(config.db_path)
    db.execute(
        "INSERT INTO apps (app_id, name, status, repo_path) VALUES (?, ?, ?, ?)",
        (app_id, name, status, repo_path),
    )
    db.commit()
    db.close()


def _write_manifest(repo_dir, *, with_tls):
    os.makedirs(repo_dir, exist_ok=True)
    toml = "[app]\nname = \"x\"\nversion = \"0.1.0\"\n\n[runtime.container]\nimage = \"Dockerfile\"\nport = 8080\n"
    if with_tls:
        toml += '\n[[tls_certs]]\nlabel = "main"\ndomains = ["{app}.{zone}"]\n'
    with open(os.path.join(repo_dir, "openhost.toml"), "w") as f:
        f.write(toml)


def test_skips_apps_without_tls_certs(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.make_all_dirs()
    _make_db(config)
    repo = tmp_path / "repo_notls"
    _write_manifest(str(repo), with_tls=False)
    _add_app(config, "app1", "notls", "running", str(repo))

    provisioned = []
    restarted = []
    monkeypatch.setattr(
        renewal,
        "provision_app_certs_for_deploy",
        lambda name, manifest, cfg: provisioned.append(name) or (None, []),
    )
    monkeypatch.setattr("compute_space.core.apps.start_app_process", lambda *a, **k: restarted.append(a))

    renewal.renew_app_certs_once(config)
    assert provisioned == []
    assert restarted == []


def test_skips_non_running_apps(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.make_all_dirs()
    _make_db(config)
    repo = tmp_path / "repo_stopped"
    _write_manifest(str(repo), with_tls=True)
    _add_app(config, "app1", "stopped", "stopped", str(repo))

    provisioned = []
    monkeypatch.setattr(
        renewal,
        "provision_app_certs_for_deploy",
        lambda name, manifest, cfg: provisioned.append(name) or (None, []),
    )
    monkeypatch.setattr("compute_space.core.apps.start_app_process", lambda *a, **k: None)

    renewal.renew_app_certs_once(config)
    assert provisioned == []


def test_no_restart_when_cert_unchanged(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.make_all_dirs()
    _make_db(config)
    repo = tmp_path / "repo_tls"
    _write_manifest(str(repo), with_tls=True)
    _add_app(config, "app1", "myapp", "running", str(repo))

    # Pre-create a cert file in the app cert dir; provisioning leaves it untouched.
    cert_dir = app_cert_dir(config.openhost_data_path, "myapp")
    cert_dir.mkdir(parents=True)
    (cert_dir / "x.crt").write_text("cert")

    restarted = []
    monkeypatch.setattr(renewal, "provision_app_certs_for_deploy", lambda name, manifest, cfg: (None, []))
    monkeypatch.setattr("compute_space.core.apps.start_app_process", lambda *a, **k: restarted.append(a))

    renewal.renew_app_certs_once(config)
    assert restarted == []


def test_restart_when_cert_changes(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.make_all_dirs()
    _make_db(config)
    repo = tmp_path / "repo_tls2"
    _write_manifest(str(repo), with_tls=True)
    _add_app(config, "app1", "myapp", "running", str(repo))

    cert_dir = app_cert_dir(config.openhost_data_path, "myapp")
    cert_dir.mkdir(parents=True)
    cert_file = cert_dir / "x.crt"
    cert_file.write_text("old")

    def fake_provision(name, manifest, cfg):
        # Simulate a renewal: rewrite the cert with a newer mtime.
        os.utime(cert_file, (cert_file.stat().st_atime + 100, cert_file.stat().st_mtime + 100))
        return (None, [])

    restarted = []
    monkeypatch.setattr(renewal, "provision_app_certs_for_deploy", fake_provision)
    monkeypatch.setattr("compute_space.core.apps.start_app_process", lambda app_id, db, cfg: restarted.append(app_id))

    renewal.renew_app_certs_once(config)
    assert restarted == ["app1"]


def test_provision_failure_isolated(tmp_path, monkeypatch):
    """A provisioning failure on one app is swallowed and does not restart it."""
    config = _config(tmp_path)
    config.make_all_dirs()
    _make_db(config)
    repo = tmp_path / "repo_fail"
    _write_manifest(str(repo), with_tls=True)
    _add_app(config, "app1", "myapp", "running", str(repo))

    def boom(name, manifest, cfg):
        raise RuntimeError("acme down")

    restarted = []
    monkeypatch.setattr(renewal, "provision_app_certs_for_deploy", boom)
    monkeypatch.setattr("compute_space.core.apps.start_app_process", lambda *a, **k: restarted.append(a))

    # Should not raise.
    renewal.renew_app_certs_once(config)
    assert restarted == []


def test_start_sweep_noop_when_tls_disabled(tmp_path, monkeypatch):
    config = _config(tmp_path).evolve(tls_enabled=False)
    started = []
    monkeypatch.setattr(renewal.threading, "Thread", lambda *a, **k: started.append(1) or _Dummy())
    renewal.start_cert_renewal_sweep(config)
    assert started == []


class _Dummy:
    def start(self):
        pass


def test_tls_package_importable():
    assert tls is not None
