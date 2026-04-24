"""Unit tests for ``compute_space.core.apps.deploy_app_background``.

The function is the in-process builder that ``/api/add_app`` and
``/api/reload`` hand off to a background thread.  It has two
non-obvious contracts that the rest of the system depends on:

1. **Transient build failures retry up to 3 times**, with escalating
   backoff (``attempt * 5`` seconds).  If the retry loop ever regresses
   to a single attempt, every flaky podman pull / transient network
   blip surfaces to the user as a hard ``status='error'`` row.

2. **``BUILD_CACHE_CORRUPT_MARKER`` short-circuits the retry loop.**
   Retrying a corrupt local build cache just burns another ~300s
   timeout per attempt before returning the same error.  The dashboard
   matches this marker to surface a "drop cache and rebuild" toast —
   dragging the user through two additional silent retries before that
   toast appears is a user-visible regression (minutes of spinner on
   every app page).

These tests mock out ``build_image`` / ``run_container`` / the sqlite
schema so they run without podman, without real sqlite migrations, and
without real sleep delays.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from compute_space.core import apps as apps_module
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.manifest import AppManifest

from .conftest import _make_test_config

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_basic_manifest() -> AppManifest:
    return AppManifest(
        name="notes",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=128,
        cpu_millicores=100,
    )


def _seed_apps_row(db_path: str, app_name: str, local_port: int) -> None:
    """Insert a minimal apps row so deploy_app_background's UPDATEs land.

    We skip the full init_db path — that drags in Quart — and just
    apply the single CREATE TABLE the function needs."""
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                local_port INTEGER NOT NULL UNIQUE,
                container_id TEXT,
                status TEXT NOT NULL DEFAULT 'stopped',
                error_message TEXT
            )"""
        )
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status)
               VALUES (?, '0.1.0', '/tmp/fake-repo', ?, 'building')""",
            (app_name, local_port),
        )
        db.commit()
    finally:
        db.close()


def _row(db_path: str, app_name: str) -> dict[str, Any]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        r = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
        assert r is not None
        return dict(r)
    finally:
        db.close()


@pytest.fixture
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record and suppress ``time.sleep`` calls inside
    ``compute_space.core.apps``.  Returns the recording list."""
    calls: list[float] = []

    def _record(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(apps_module.time, "sleep", _record)
    return calls


@pytest.fixture
def stub_downstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the downstream side-effects of deploy_app_background
    (storage check, run_container, wait_for_ready) with no-op stubs.

    Returns a dict the test can inspect to confirm which stubs were
    actually called."""
    called: dict[str, Any] = {}

    def _noop_storage_check(_cfg: Any) -> None:
        called["storage_check"] = True

    def _fake_run_container(*args: Any, **kwargs: Any) -> str:
        called["run_container_argv"] = (args, kwargs)
        return "container-id-xyz"

    def _fake_wait_for_ready(*args: Any, **kwargs: Any) -> bool:
        called["wait_for_ready"] = True
        return True

    monkeypatch.setattr(apps_module.storage, "check_before_deploy", _noop_storage_check)
    monkeypatch.setattr(apps_module, "run_container", _fake_run_container)
    monkeypatch.setattr(apps_module, "wait_for_ready", _fake_wait_for_ready)
    return called


# ---------------------------------------------------------------------------
# Retry / short-circuit tests
# ---------------------------------------------------------------------------


def test_generic_build_failure_retries_three_times_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], stub_downstream: dict[str, Any]
) -> None:
    """Two transient failures followed by a success must leave the app
    in ``status='running'`` — i.e. the retry loop must try ≥3 times
    before giving up, and a success on the last allowed attempt must
    still land the happy path."""
    cfg = _make_test_config(tmp_path, port=19500)
    _seed_apps_row(cfg.db_path, "notes", 19501)

    attempts = {"n": 0}

    def _flaky_build_image(*args: Any, **kwargs: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("pull failed: connection reset")
        return "openhost-notes:latest"

    monkeypatch.setattr(apps_module, "build_image", _flaky_build_image)

    apps_module.deploy_app_background(
        manifest=_make_basic_manifest(),
        repo_path="/tmp/fake-repo",
        local_port=19501,
        env_vars={},
        config=cfg,
    )

    # Exactly 3 build attempts: fail, fail, succeed.
    assert attempts["n"] == 3
    # Backoff between the two retries: attempt * 5 seconds, i.e. 5 then 10.
    # No sleep after the final successful attempt.
    assert sleep_calls == [5, 10]
    # run_container + wait_for_ready fired on the happy path.
    assert stub_downstream.get("run_container_argv") is not None
    assert stub_downstream.get("wait_for_ready") is True
    # DB row reflects the final running state.
    row = _row(cfg.db_path, "notes")
    assert row["status"] == "running"
    assert row["error_message"] is None


def test_generic_build_failure_gives_up_after_three_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], stub_downstream: dict[str, Any]
) -> None:
    """If all three attempts fail, the app row ends up in status='error'
    with the original exception message and the retry cadence we promise
    on the tin (5s, 10s, no final sleep because the error propagates)."""
    cfg = _make_test_config(tmp_path, port=19502)
    _seed_apps_row(cfg.db_path, "notes", 19503)

    attempts = {"n": 0}

    def _always_fail(*args: Any, **kwargs: Any) -> str:
        attempts["n"] += 1
        raise RuntimeError("pull failed: connection reset")

    monkeypatch.setattr(apps_module, "build_image", _always_fail)

    # deploy_app_background swallows the final exception itself (it
    # logs + writes an 'error' row), so this call must not raise.
    apps_module.deploy_app_background(
        manifest=_make_basic_manifest(),
        repo_path="/tmp/fake-repo",
        local_port=19503,
        env_vars={},
        config=cfg,
    )

    assert attempts["n"] == 3, "must attempt exactly 3 times before giving up"
    # Backoffs before attempts 2 and 3; no backoff after the final failure
    # because we re-raise out of the loop.
    assert sleep_calls == [5, 10]
    # run_container must never be called when build never succeeds.
    assert "run_container_argv" not in stub_downstream
    # Error recorded on the row.
    row = _row(cfg.db_path, "notes")
    assert row["status"] == "error"
    assert "pull failed: connection reset" in (row["error_message"] or "")


def test_cache_corrupt_marker_short_circuits_retry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], stub_downstream: dict[str, Any]
) -> None:
    """A build error carrying ``BUILD_CACHE_CORRUPT_MARKER`` must
    propagate on the first attempt without sleeping or retrying.

    Retrying can't heal a corrupt local cache, and the dashboard
    matches on the marker in ``error_message`` to fire a 'drop cache
    and rebuild' toast — two extra retries before that toast appears
    would show the user a minutes-long spinner for no benefit."""
    cfg = _make_test_config(tmp_path, port=19504)
    _seed_apps_row(cfg.db_path, "notes", 19505)

    attempts = {"n": 0}

    def _cache_corrupt(*args: Any, **kwargs: Any) -> str:
        attempts["n"] += 1
        raise RuntimeError(f"{BUILD_CACHE_CORRUPT_MARKER} content digest sha256:deadbeef: not found")

    monkeypatch.setattr(apps_module, "build_image", _cache_corrupt)

    apps_module.deploy_app_background(
        manifest=_make_basic_manifest(),
        repo_path="/tmp/fake-repo",
        local_port=19505,
        env_vars={},
        config=cfg,
    )

    assert attempts["n"] == 1, "cache-corrupt marker must short-circuit after 1 attempt"
    assert sleep_calls == [], "no retry backoff when short-circuiting"
    assert "run_container_argv" not in stub_downstream
    row = _row(cfg.db_path, "notes")
    assert row["status"] == "error"
    # The marker MUST be preserved in error_message so the dashboard
    # can match on it.
    assert BUILD_CACHE_CORRUPT_MARKER in (row["error_message"] or "")


def test_cache_corrupt_marker_embedded_mid_message_still_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], stub_downstream: dict[str, Any]
) -> None:
    """The marker detection uses substring matching (``MARKER in str(e)``),
    so a message that surfaces the marker anywhere in the exception
    body — not just at the start — must still short-circuit.

    This guards against future refactors that might switch to
    ``str(e).startswith(MARKER)`` and silently let cache-corrupt
    errors fall through to the retry loop when the build system
    prefixes extra context."""
    cfg = _make_test_config(tmp_path, port=19506)
    _seed_apps_row(cfg.db_path, "notes", 19507)

    attempts = {"n": 0}

    def _fail_with_embedded_marker(*args: Any, **kwargs: Any) -> str:
        attempts["n"] += 1
        raise RuntimeError(
            "build step 4/10 failed: podman build crashed. "
            f"{BUILD_CACHE_CORRUPT_MARKER} content digest sha256:xx: not found"
        )

    monkeypatch.setattr(apps_module, "build_image", _fail_with_embedded_marker)

    apps_module.deploy_app_background(
        manifest=_make_basic_manifest(),
        repo_path="/tmp/fake-repo",
        local_port=19507,
        env_vars={},
        config=cfg,
    )

    assert attempts["n"] == 1
    assert sleep_calls == []
    row = _row(cfg.db_path, "notes")
    assert row["status"] == "error"
    assert BUILD_CACHE_CORRUPT_MARKER in (row["error_message"] or "")


def test_happy_path_does_not_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], stub_downstream: dict[str, Any]
) -> None:
    """When the first build attempt succeeds, the loop must not sleep
    at all.  A regression that unconditionally sleeps before returning
    would add 5s of latency to every deploy — a silent regression
    nothing else would flag."""
    cfg = _make_test_config(tmp_path, port=19508)
    _seed_apps_row(cfg.db_path, "notes", 19509)

    def _succeed(*args: Any, **kwargs: Any) -> str:
        return "openhost-notes:latest"

    monkeypatch.setattr(apps_module, "build_image", _succeed)

    apps_module.deploy_app_background(
        manifest=_make_basic_manifest(),
        repo_path="/tmp/fake-repo",
        local_port=19509,
        env_vars={},
        config=cfg,
    )

    assert sleep_calls == []
    row = _row(cfg.db_path, "notes")
    assert row["status"] == "running"
