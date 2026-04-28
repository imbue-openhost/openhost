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
from unittest.mock import MagicMock
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
    try:
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
            "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) "
            "VALUES (?, 'http', 8080, 19501)",
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
    finally:
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


def test_remove_returns_quietly_when_app_already_gone(tmp_path: Path) -> None:
    """Calling the worker for a non-existent app must be a clean no-op.

    Reachable when startup recovery spawns a worker for a row that a
    fresh user request finished removing in the same window. The worker
    sees ``app_row is None`` and returns before invoking any teardown
    helpers.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    # No app seeded.

    with (
        patch("compute_space.core.apps.stop_app_process") as stop,
        patch("compute_space.core.apps.remove_image") as rmi,
        patch("compute_space.core.apps.deprovision_data") as full,
        patch("compute_space.core.apps.deprovision_temp_data") as temp_only,
    ):
        # Must not raise.
        remove_app_background("ghost", keep_data=False, config=cfg)

    stop.assert_not_called()
    rmi.assert_not_called()
    full.assert_not_called()
    temp_only.assert_not_called()


def test_remove_records_error_when_db_delete_path_explodes(tmp_path: Path) -> None:
    """If the DELETE itself fails, the row should land in 'error' so
    the operator isn't left staring at a permanent 'removing' indicator.

    Stop/remove/deprovision failures are caught by inner handlers so the
    DELETE proceeds normally; this test exercises the OUTER except
    handler, which only fires when something escapes those inner
    handlers — most realistically a SQLite error on the DELETE itself.
    We trigger that by wrapping the connection so DELETE raises.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    real_connect = sqlite3.connect

    class _ExplodingConn:
        """Wrapper that raises ``OperationalError`` on ``DELETE FROM apps``
        and forwards every other call to the real connection.

        ``__setattr__`` and ``__getattr__`` both forward through to the
        real connection so that mutations like ``db.row_factory =
        sqlite3.Row`` actually take effect on the underlying connection
        — otherwise reads would come back as plain tuples and the test
        would exercise an unintended TypeError path before ever hitting
        the simulated DELETE failure.
        """

        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, *args, **kwargs):
            sql = args[0] if args else ""
            if sql.startswith("DELETE FROM apps"):
                raise sqlite3.OperationalError("simulated DELETE failure")
            return self._real.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

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


def test_resume_pending_removals_null_keep_data_defaults_to_full(tmp_path: Path) -> None:
    """If ``removing_keep_data`` is NULL on a recovery row (an anomalous
    DB state — the route always sets the column), the worker must
    default to the FULL deprovision path (keep_data=False).

    Rationale: the user only ever reaches the removal flow via an
    explicit confirmation, and "Keep Data" is the opt-in branch.
    Defaulting to True (keep) would silently leave files on disk that
    the user expected to be deleted, which is the behaviour we want to
    avoid. This test pins that choice so a future refactor doesn't
    flip the default.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET removing_keep_data = NULL WHERE name = ?", ("myapp",))
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

    temp_only.assert_not_called()
    full.assert_called_once_with("myapp", cfg.persistent_data_dir, cfg.temporary_data_dir)


def test_remove_records_error_when_initial_connect_fails(tmp_path: Path) -> None:
    """If even ``sqlite3.connect()`` raises (locked DB, permissions,
    disk full), the worker still flips the row to 'error' through a
    fresh connection so it doesn't sit stuck in 'removing' forever.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")

    real_connect = sqlite3.connect
    calls = {"n": 0}

    def _connect_side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call (worker's main connection) — fail.
            raise sqlite3.OperationalError("simulated initial connect failure")
        # Second call (recovery connection inside outer except) — succeed.
        return real_connect(*args, **kwargs)

    with patch("compute_space.core.apps.sqlite3.connect", side_effect=_connect_side_effect):
        # Must not raise; must still flip the row to error.
        remove_app_background("myapp", keep_data=False, config=cfg)

    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status, removing_keep_data FROM apps WHERE name = 'myapp'").fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "error"
    assert row[1] is None


def test_resume_pending_removals_swallows_db_errors(tmp_path: Path) -> None:
    """A DB error during startup recovery must not prevent server boot.

    Rationale: ``_resume_pending_removals`` is invoked from ``init_app``,
    so any unhandled exception aborts the entire startup. The function
    should log and return, leaving any 'removing' rows for an operator
    to retry from the dashboard.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    # Don't seed any app — instead make the SELECT fail.
    with patch(
        "compute_space.core.startup.sqlite3.connect",
        side_effect=sqlite3.OperationalError("disk on fire"),
    ):
        # Must NOT raise.
        _resume_pending_removals(cfg)


def test_resume_pending_removals_survives_per_row_thread_spawn_failure(tmp_path: Path) -> None:
    """A per-row Thread.start() failure must be caught so that startup
    continues. Without this, a single hostile row (or genuinely
    resource-exhausted host) would crash init_app and prevent the
    server from coming up at all — far worse than leaving the row in
    a recoverable state.

    The recovery state itself must be ``status='error'``, NOT
    'removing': the route's atomic-claim guard
    ``WHERE status != 'removing'`` would refuse every dashboard retry
    if the row were left in 'removing', forcing the operator to wait
    for another server restart to re-run this function. Flipping to
    'error' mirrors the route handler's own thread-spawn-failure
    recovery and unsticks the row for an immediate retry.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_with_children(cfg.db_path, "myapp")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET removing_keep_data = 0 WHERE name = ?", ("myapp",))
    db.commit()
    db.close()

    failing_thread = MagicMock()
    failing_thread.return_value.start.side_effect = RuntimeError("can't start new thread")

    with patch("compute_space.core.startup.threading.Thread", failing_thread):
        # Must NOT raise — the per-row guard catches the failure.
        _resume_pending_removals(cfg)

    # Row is flipped to 'error' so a dashboard retry can re-claim it
    # via the atomic UPDATE in /remove_app.
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status, removing_keep_data FROM apps WHERE name = 'myapp'").fetchone()
    finally:
        db.close()
    assert row[0] == "error"
    assert row[1] is None
