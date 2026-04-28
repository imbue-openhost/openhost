"""Tests for :func:`compute_space.core.apps.remove_app_background`.

These cover the bits that are easy to verify without spawning a
container runtime: foreign-key cascade behaviour, error-path bookkeeping,
and startup recovery picking up a row stuck in ``status='removing'``.
The container/image teardown side effects are exercised end-to-end in
``test_integration.py`` under the ``--run-containers`` marker.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from compute_space.core.apps import remove_app_background
from compute_space.core.startup import _resume_pending_removals
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


def _seed_app_with_children(db_path: str, app_name: str = "myapp") -> None:
    """Insert an app row and one row in each cascade-target table.

    Used by the FK-cascade test: after remove_app_background runs, all
    the child rows must be gone, otherwise a future deploy with the
    same name (which is allowed once the row is removed) would collide
    with leftover permissions / ports / tokens.
    """
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "INSERT INTO apps (name, version, repo_path, local_port, status) "
        "VALUES (?, '1.0', '/repo', 19500, 'removing')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO app_databases (app_name, db_name, db_path) VALUES (?, 'main', '/data/main.db')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES (?, 'http', 8080, 19501)",
        (app_name,),
    )
    db.execute(
        "INSERT INTO app_tokens (app_name, token_hash) VALUES (?, 'fakehash')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO service_providers (service_name, app_name) VALUES ('s1', ?)",
        (app_name,),
    )
    db.execute(
        "INSERT INTO service_providers_v2 (service_url, app_name, service_version, endpoint) "
        "VALUES ('https://e.x/s', ?, '1.0', '/svc')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO permissions (consumer_app, permission_key) VALUES (?, 'k')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO permissions_v2 (consumer_app, service_url, grant_payload) VALUES (?, 'u', '{}')",
        (app_name,),
    )
    db.execute(
        "INSERT INTO service_defaults (service_url, app_name) VALUES ('https://e.x/s', ?)",
        (app_name,),
    )
    db.commit()
    db.close()


def _table_has_app(db_path: str, table: str, app_name: str, key_col: str = "app_name") -> bool:
    db = sqlite3.connect(db_path)
    try:
        cur = db.execute(f"SELECT 1 FROM {table} WHERE {key_col} = ?", (app_name,))
        return cur.fetchone() is not None
    finally:
        db.close()


def test_remove_cascades_to_all_child_tables(tmp_path: Path) -> None:
    """``DELETE FROM apps`` must cascade to every ON-DELETE-CASCADE child.

    SQLite enforces foreign keys per-connection and defaults to OFF, so
    the worker's own connection must explicitly ``PRAGMA foreign_keys =
    ON`` before the DELETE. If it doesn't, the cascade triggers silently
    no-op and orphan rows accumulate in app_tokens / permissions /
    service_providers / port_mappings, breaking re-deploys of the same
    name (UNIQUE conflicts on host_port etc.) and leaking auth tokens.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    # Patch out the side-effecty bits so we can run this without podman.
    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data"),
        patch("compute_space.core.apps.deprovision_temp_data"),
    ):
        remove_app_background("myapp", keep_data=False, config=cfg)

    # The apps row itself.
    assert not _table_has_app(cfg.db_path, "apps", "myapp", key_col="name"), "apps row was not deleted"
    # Every other table is cleared via the FK cascade triggered by
    # ``DELETE FROM apps``. If foreign-key enforcement were off (the
    # bug this test guards), all of these rows would still be present.
    for table, key_col in [
        ("app_databases", "app_name"),
        ("app_port_mappings", "app_name"),
        ("app_tokens", "app_name"),
        ("service_providers", "app_name"),
        ("service_providers_v2", "app_name"),
        ("permissions", "consumer_app"),
        ("permissions_v2", "consumer_app"),
        ("service_defaults", "app_name"),
    ]:
        assert not _table_has_app(cfg.db_path, table, "myapp", key_col=key_col), (
            f"{table}.{key_col} still has a row for 'myapp' — FK cascade did not fire"
        )


def test_remove_keep_data_calls_temp_only(tmp_path: Path) -> None:
    """``keep_data=True`` must hit the temp-only deprovision path.

    The two functions clean different directories; getting this wrong
    means user data is either leaked (full delete called for keep) or
    orphaned (temp-only called when full delete intended).
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        remove_app_background("myapp", keep_data=True, config=cfg)

    full.assert_not_called()
    temp_only.assert_called_once_with("myapp", cfg.temporary_data_dir)


def test_remove_full_calls_full_deprovision(tmp_path: Path) -> None:
    """``keep_data=False`` must hit the full deprovision (mirror of above)."""
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        remove_app_background("myapp", keep_data=False, config=cfg)

    temp_only.assert_not_called()
    full.assert_called_once_with("myapp", cfg.persistent_data_dir, cfg.temporary_data_dir)


def test_remove_proceeds_when_deprovision_raises(tmp_path: Path) -> None:
    """A deprovision failure must not block the row delete.

    Otherwise a stuck data dir (e.g. a permission glitch) would leave
    the app row in 'removing' forever and the user could never re-deploy.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch(
            "compute_space.core.apps.deprovision_data",
            side_effect=OSError("disk on fire"),
        ),
        patch("compute_space.core.apps.deprovision_temp_data"),
    ):
        remove_app_background("myapp", keep_data=False, config=cfg)

    assert not _table_has_app(cfg.db_path, "apps", "myapp", key_col="name")


def test_remove_records_error_when_db_delete_path_explodes(tmp_path: Path) -> None:
    """If something *inside the worker's outer try* blows up (not just a
    deprovision hiccup), the row should land in 'error' so the operator
    isn't left staring at a permanent 'removing' indicator."""
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    # Make stop_app_process explode inside a way that's caught locally,
    # but also make remove_image trigger something the outer except will
    # see — easiest is to patch the sqlite3.Connection.execute used by
    # the DELETE step. We simulate that via a side-effect on remove_image
    # that *itself* raises a non-Exception type; instead, make the DELETE
    # path raise by patching remove_image to mutate the connection state.
    # Simplest: patch deprovision_data to raise BaseException so the
    # narrow ``except Exception`` in the worker doesn't catch it and
    # the outer ``except Exception`` records the failure. BaseException
    # IS caught by ``except Exception:`` though. Use a custom exception
    # raised after the deprovision try-block — patch deprovision_data
    # to mutate a flag and then patch the DELETE step directly. The
    # easiest reliable signal is to replace ``sqlite3.connect`` inside
    # the function so the second .execute() call raises.
    real_connect = sqlite3.connect
    call_log: list[str] = []

    class _ExplodingConn:
        def __init__(self, real):
            self._real = real

        def execute(self, *args, **kwargs):
            sql = args[0] if args else ""
            call_log.append(sql)
            if sql.startswith("DELETE FROM apps"):
                raise sqlite3.OperationalError("simulated DELETE failure")
            return self._real.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _wrap(*args, **kwargs):
        return _ExplodingConn(real_connect(*args, **kwargs))

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data"),
        patch("compute_space.core.apps.deprovision_temp_data"),
        patch("compute_space.core.apps.sqlite3.connect", side_effect=_wrap),
    ):
        remove_app_background("myapp", keep_data=False, config=cfg)

    # Row should still exist (DELETE failed) but flipped to error.
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute(
            "SELECT status, error_message, removing_keep_data FROM apps WHERE name = ?",
            ("myapp",),
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "error"
    assert "Removal failed" in (row[1] or "")
    assert row[2] is None  # cleared so a retry can re-claim


def _wait_for_app_gone(db_path: str, app_name: str, timeout: float = 5) -> bool:
    """Poll ``apps`` until the given row vanishes or ``timeout`` elapses.

    ``_resume_pending_removals`` spawns daemon threads so there's no
    handle to ``join()``; polling the DB row is the cleanest way to
    detect completion in a unit test.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = sqlite3.connect(db_path)
        try:
            gone = db.execute("SELECT 1 FROM apps WHERE name = ?", (app_name,)).fetchone() is None
        finally:
            db.close()
        if gone:
            return True
        time.sleep(0.05)
    return False


def test_resume_pending_removals_finishes_interrupted_remove(tmp_path: Path) -> None:
    """A row left in 'removing' across a restart must be picked back up."""
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")
    # Set the flag the route would have written before the crash.
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET removing_keep_data = 0 WHERE name = ?", ("myapp",))
    db.commit()
    db.close()

    # Patch the heavy bits so this is a unit test, not an integration test.
    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data"),
        patch("compute_space.core.apps.deprovision_temp_data"),
    ):
        _resume_pending_removals(cfg)
        if not _wait_for_app_gone(cfg.db_path, "myapp"):
            pytest.fail("Pending removal was not finished by _resume_pending_removals")


def test_resume_pending_removals_keep_data_uses_temp_only(tmp_path: Path) -> None:
    """Crash recovery must honour the ``keep_data`` choice persisted by
    the original /remove_app request — otherwise a crash mid-removal
    silently flips a "Remove (Keep Data)" into a full delete and the
    user loses their persistent data dir.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET removing_keep_data = 1 WHERE name = ?", ("myapp",))
    db.commit()
    db.close()

    with (
        patch("compute_space.core.apps.stop_app_process"),
        patch("compute_space.core.apps.remove_image"),
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        _resume_pending_removals(cfg)
        if not _wait_for_app_gone(cfg.db_path, "myapp"):
            pytest.fail("Pending removal was not finished by _resume_pending_removals")

    full.assert_not_called()
    temp_only.assert_called_once_with("myapp", cfg.temporary_data_dir)
