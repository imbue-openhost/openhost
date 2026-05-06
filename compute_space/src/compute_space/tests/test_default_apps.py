"""Tests for the auto-deploy-default-apps hook.

Three properties matter:

1. **Idempotency**: an app that successfully installs once is never
   re-installed across boots.  The sentinel records "ok" for it; the
   next call returns "skipped" without touching the DB.

2. **Retry budget**: an app that fails gets retried up to
   ``MAX_RETRY_ATTEMPTS`` total times, then is marked "failed" and
   skipped.  Operators can clear by deleting the sentinel.

3. **No-op + safety paths**: empty ``default_apps``, missing builtin
   directories, and ``insert_and_deploy`` raising must not propagate
   out of ``deploy_default_apps``.

The tests stub ``insert_and_deploy`` so we don't need a real podman
runtime; we only verify the surrounding bookkeeping.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core import default_apps as da
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
    """Bootstrap a sqlite file with the production schema (so apps
    inserts have somewhere to go)."""
    conn = sqlite3.connect(db_path)
    try:
        with open(_schema_path()) as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def _make_app_dir(apps_dir: Path, dir_name: str, manifest_name: str | None = None) -> Path:
    """Create a minimal builtin app directory with a parseable
    openhost.toml."""
    app_dir = apps_dir / dir_name
    app_dir.mkdir(parents=True)
    name = manifest_name or dir_name
    (app_dir / "openhost.toml").write_text(
        f"[app]\n"
        f'name = "{name}"\n'
        f'version = "0.1"\n'
        f"\n"
        f"[runtime.container]\n"
        f'image = "Dockerfile"\n'
        f"port = 8080\n"
        f"\n"
        f"[resources]\n"
        f"memory_mb = 128\n"
        f"cpu_millicores = 100\n"
        f"\n"
        f"[routing]\n"
        f'health_check = "/"\n'
    )
    (app_dir / "Dockerfile").write_text("FROM alpine\n")
    return app_dir


@pytest.fixture
def cfg_with_apps(tmp_path: Path):
    """Build a Config whose ``apps_dir`` contains two builtin apps,
    plus a fresh sqlite DB at the canonical path."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    _make_app_dir(apps_dir, "secrets-v2")
    _make_app_dir(apps_dir, "file-browser")

    cfg = _make_cfg(
        tmp_path,
        apps_dir=apps_dir,
        default_apps=["secrets-v2", "file-browser"],
    )
    _seed_db(cfg.db_path)
    return cfg


def _patch_insert_and_deploy(monkeypatch: pytest.MonkeyPatch, *, fail_for: set[str] | None = None):
    """Stub ``insert_and_deploy`` to write a row + return the name,
    skipping the daemon-thread podman build.  ``fail_for`` is a set
    of manifest names that should raise instead of inserting."""
    fail_for = fail_for or set()

    def fake(manifest, repo_path, config, db, **kwargs):  # type: ignore[no-untyped-def]
        if manifest.name in fail_for:
            raise RuntimeError(f"simulated failure for {manifest.name}")
        # Allocate a fresh local_port so the sqlite UNIQUE constraint
        # doesn't fire when the test installs more than one app.
        next_port = db.execute("SELECT COALESCE(MAX(local_port), 18999) + 1 FROM apps").fetchone()[0]
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status) VALUES (?, ?, ?, ?, 'building')",
            (manifest.name, manifest.version or "0.1", repo_path, next_port),
        )
        db.commit()
        return manifest.name

    monkeypatch.setattr(da, "insert_and_deploy", fake)


def _read_sentinel(cfg) -> dict:
    if not os.path.isfile(cfg.default_apps_sentinel_path):
        return {}
    with open(cfg.default_apps_sentinel_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Happy path: deploy two apps, idempotent on re-call
# ---------------------------------------------------------------------------


def test_deploy_default_apps_installs_each(cfg_with_apps, monkeypatch):
    """Both configured apps end up with status='ok' after a fresh
    deploy, and the sentinel records both as ok."""
    _patch_insert_and_deploy(monkeypatch)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert result.ok_count == 2
    assert result.failed_count == 0
    statuses = {o.name: o.status for o in result.outcomes}
    assert statuses == {"secrets-v2": "ok", "file-browser": "ok"}

    sentinel = _read_sentinel(cfg_with_apps)
    assert set(sentinel.keys()) == {"secrets-v2", "file-browser"}
    assert all(entry["status"] == "ok" for entry in sentinel.values())


def test_redeploy_short_circuits_on_terminal_sentinel(cfg_with_apps, monkeypatch):
    """A second call after a successful first call must NOT re-walk
    apps_dir, NOT re-parse manifests, and NOT call insert_and_deploy
    again — the sentinel is the source of truth for "already done"."""
    _patch_insert_and_deploy(monkeypatch)
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)

        # If anything tries to re-install, blow up loudly.
        def must_not_run(*args, **kwargs):
            raise AssertionError("insert_and_deploy called on terminal-sentinel app")

        monkeypatch.setattr(da, "insert_and_deploy", must_not_run)

        # Same for _install_one — even reading the manifest is wasted
        # work once the sentinel says we're done.
        def must_not_install_one(*args, **kwargs):
            raise AssertionError("_install_one called on terminal-sentinel app")

        monkeypatch.setattr(da, "_install_one", must_not_install_one)

        result2 = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    # Every app reports its persisted prior status (ok), not "skipped".
    # ``skipped`` is reserved for the (rarer) case where the DB row
    # already existed before the hook ever fired — we record that
    # too and treat it as terminal.
    assert all(o.status == "ok" for o in result2.outcomes)


def test_existing_db_row_short_circuits_install(cfg_with_apps, monkeypatch):
    """An app whose name already has a DB row (e.g. operator manually
    installed it) must be reported 'skipped', not 'failed'."""
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status) "
            "VALUES (?, '0.1', '/r/secrets-v2', 9100, 'running')",
            ("secrets-v2",),
        )
        db.commit()
        _patch_insert_and_deploy(monkeypatch)

        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    by_name = {o.name: o for o in result.outcomes}
    assert by_name["secrets-v2"].status == "skipped"
    assert by_name["file-browser"].status == "ok"


# ---------------------------------------------------------------------------
# Failure handling: retries, sentinel persistence, error capture
# ---------------------------------------------------------------------------


def test_failure_records_attempt_count_and_error(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2"})
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    by_name = {o.name: o for o in result.outcomes}
    assert by_name["secrets-v2"].status == "failed"
    assert by_name["secrets-v2"].attempts == 1
    assert by_name["secrets-v2"].error is not None
    assert by_name["file-browser"].status == "ok"


def test_retries_until_max_attempts(cfg_with_apps, monkeypatch):
    """A persistently-failing app gets retried up to MAX_RETRY_ATTEMPTS
    times across deploy_default_apps invocations, then short-circuits."""
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2", "file-browser"})

    for i in range(da.MAX_RETRY_ATTEMPTS):
        db = sqlite3.connect(cfg_with_apps.db_path)
        try:
            result = da.deploy_default_apps(cfg_with_apps, db)
        finally:
            db.close()
        for o in result.outcomes:
            assert o.attempts == i + 1, f"after call {i + 1}: {o}"
            assert o.status == "failed"

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result_after = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    for o in result_after.outcomes:
        assert o.attempts == da.MAX_RETRY_ATTEMPTS
        assert o.status == "failed"


def test_retry_succeeds_on_second_attempt(cfg_with_apps, monkeypatch):
    """An app that failed once can succeed on the next call (matches
    the 'transient podman error -> retry next boot' real-world path)."""
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2"})
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    sentinel = _read_sentinel(cfg_with_apps)
    assert sentinel["secrets-v2"]["status"] == "failed"

    _patch_insert_and_deploy(monkeypatch, fail_for=set())
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    by_name = {o.name: o for o in result.outcomes}
    assert by_name["secrets-v2"].status == "ok"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_default_apps_is_no_op(tmp_path: Path):
    """An operator who set ``default_apps = []`` to opt out gets a
    no-op: no sentinel written, no DB rows created, no errors."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=[])
    _seed_db(cfg.db_path)

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert result.outcomes == []
    assert not os.path.isfile(cfg.default_apps_sentinel_path)


def test_missing_builtin_dir_records_failure(tmp_path: Path):
    """A default_apps entry that doesn't exist on disk must surface
    as a failure (caught + recorded), not a crash."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=["does-not-exist"])
    _seed_db(cfg.db_path)

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "failed"
    assert "not found" in (result.outcomes[0].error or "")


def test_corrupt_sentinel_is_treated_as_empty(cfg_with_apps, monkeypatch):
    """A truncated/garbage sentinel must not crash the deploy hook;
    we silently treat it as empty and re-run from scratch."""
    os.makedirs(os.path.dirname(cfg_with_apps.default_apps_sentinel_path), exist_ok=True)
    with open(cfg_with_apps.default_apps_sentinel_path, "w") as f:
        f.write("not-json{{{")
    _patch_insert_and_deploy(monkeypatch)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert result.ok_count == 2


def test_non_numeric_attempts_does_not_crash(cfg_with_apps, monkeypatch):
    """A hand-edited sentinel with ``attempts: "lots"`` (or null,
    or a list) must not blow up the deploy hook on the next call.
    Falls back to attempts=0 so the retry budget restarts from
    scratch — better than refusing to ever retry."""
    os.makedirs(os.path.dirname(cfg_with_apps.default_apps_sentinel_path), exist_ok=True)
    with open(cfg_with_apps.default_apps_sentinel_path, "w") as f:
        json.dump(
            {
                "secrets-v2": {"status": "failed", "attempts": "garbage"},
                "file-browser": {"status": "failed", "attempts": None},
            },
            f,
        )
    _patch_insert_and_deploy(monkeypatch)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert result.ok_count == 2
    sentinel = _read_sentinel(cfg_with_apps)
    assert sentinel["secrets-v2"]["attempts"] == 1
    assert sentinel["file-browser"]["attempts"] == 1


def test_skipped_sentinel_is_terminal(cfg_with_apps, monkeypatch):
    """When the sentinel says an app was previously skipped (because
    the DB row already existed before the hook ran), we trust that
    and don't re-walk apps_dir on the next call."""
    os.makedirs(os.path.dirname(cfg_with_apps.default_apps_sentinel_path), exist_ok=True)
    with open(cfg_with_apps.default_apps_sentinel_path, "w") as f:
        json.dump(
            {
                "secrets-v2": {"status": "skipped", "attempts": 0},
                "file-browser": {"status": "ok", "attempts": 1},
            },
            f,
        )

    def must_not_run(*args, **kwargs):
        raise AssertionError("re-walked apps_dir on terminal-sentinel app")

    monkeypatch.setattr(da, "_install_one", must_not_run)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    by_name = {o.name: o for o in result.outcomes}
    assert by_name["secrets-v2"].status == "skipped"
    assert by_name["file-browser"].status == "ok"


def test_sentinel_survives_partial_failure(cfg_with_apps, monkeypatch):
    """When one app succeeds and another fails, the sentinel records
    both states; a subsequent call only retries the failure."""
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2"})
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    sentinel = _read_sentinel(cfg_with_apps)
    assert sentinel["file-browser"]["status"] == "ok"
    assert sentinel["secrets-v2"]["status"] == "failed"

    seen_names = []
    real_install = da._install_one

    def tracking_install(dir_name, config, db_):  # type: ignore[no-untyped-def]
        seen_names.append(dir_name)
        return real_install(dir_name, config, db_)

    monkeypatch.setattr(da, "_install_one", tracking_install)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert seen_names == ["secrets-v2"]
