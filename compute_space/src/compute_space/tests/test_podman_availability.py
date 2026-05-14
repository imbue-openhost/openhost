"""Tests for the runtime-availability safety net.

Existing Docker-era instances that click the dashboard Update button
end up running the new code against a host that has no podman
installed.  The safety net has three pieces, covered here:

- ``core.containers.container_runtime_available()`` reports False on missing/
  unusable podman without raising.
- ``core.containers.is_container_running()`` returns ``False``
  on missing podman instead of propagating FileNotFoundError.
- ``core.startup._check_app_status()`` detects podman missing,
  marks every running/starting/building app as ``status='error'``
  with the CONTAINER_RUNTIME_MISSING_ERROR remediation, and DOES NOT attempt
  a rebuild that would crash the router.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from compute_space.core import startup as startup_mod
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.containers import container_runtime_available
from compute_space.core.containers import is_container_running
from compute_space.db.connection import init_db as real_init_db

from .conftest import _make_test_config

# ---------------------------------------------------------------------------
# container_runtime_available
# ---------------------------------------------------------------------------


def test_podman_available_returns_true_on_successful_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, timeout):  # type: ignore[no-untyped-def]
        class _R:
            returncode = 0
            stdout = b"podman version 4.9.3\n"

        assert cmd == ["podman", "--version"]
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert container_runtime_available() is True


def test_podman_available_returns_false_on_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact failure mode on a Docker-era host that just ran self-update."""

    def fake_run(cmd, capture_output, timeout):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(2, "No such file or directory: 'podman'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert container_runtime_available() is False


def test_podman_available_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Podman hang on a broken rootless setup must not hang the caller."""

    def fake_run(cmd, capture_output, timeout):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert container_runtime_available() is False


def test_podman_available_returns_false_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, timeout):  # type: ignore[no-untyped-def]
        class _R:
            returncode = 1
            stdout = b""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert container_runtime_available() is False


def test_podman_available_returns_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """EPERM / fd exhaustion / similar odd failure modes must not crash
    the caller; container_runtime_available must catch OSError and return False
    rather than propagating, because the downstream caller in
    _check_app_status treats any exception as fatal.  (The WARNING
    log that accompanies this path is visible in journalctl but
    awkward to capture in tests because the project uses loguru; we
    pin behavior via return-value here and the log-message string is
    covered by reading the source."""

    def fake_run(cmd, capture_output, timeout):  # type: ignore[no-untyped-def]
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert container_runtime_available() is False


# ---------------------------------------------------------------------------
# is_container_running
# ---------------------------------------------------------------------------


def test_is_container_running_returns_false_on_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing podman binary must return ``False`` rather than propagating
    FileNotFoundError, so ``_check_app_status`` can detect the condition
    and mark apps with the proper remediation instead of crashing the
    router."""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(2, "No such file or directory: 'podman'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False


def test_is_container_running_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False


def test_is_container_running_returns_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetric to container_runtime_available's OSError handling.  An unexpected
    OSError from subprocess.run must not propagate — the caller in
    _check_app_status would treat it as fatal.  (Loguru's log is
    emitted and visible in journalctl but not captured via caplog, so
    this test pins the return-value contract; the WARNING message
    string is verified by code review.)"""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False


# ---------------------------------------------------------------------------
# _check_app_status
# ---------------------------------------------------------------------------


def _insert_app(db_path: str, name: str, status: str, container_id: str | None, *, local_port: int) -> None:
    """Insert a minimal apps row.  ``local_port`` is required and must be
    unique within each test's DB (the schema enforces a UNIQUE constraint);
    callers pass explicit integers rather than deriving from ``hash(name)``
    because Python's process-random string hashing made that derivation
    flaky across runs due to collisions modulo 1000."""
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_app_id(), name, "1.0", f"/repo/{name}", local_port, status, container_id),
        )
        db.commit()
    finally:
        db.close()


def _init_schema(db_path: str) -> None:
    """Create the minimal apps schema we need for these tests."""
    real_init_db(db_path)


def test_check_app_status_marks_running_apps_error_when_podman_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core guarantee: a Docker-era instance that self-updated,
    lost podman, and restarted must not crash the router.  Instead
    every running app flips to 'error' with the remediation message,
    the dashboard stays up, and no rebuild is attempted."""
    config = _make_test_config(tmp_path, port=18500)
    _init_schema(config.db_path)
    _insert_app(config.db_path, "notes", "running", "docker-id-1", local_port=9100)
    _insert_app(config.db_path, "wiki", "starting", "docker-id-2", local_port=9101)
    _insert_app(config.db_path, "blog", "building", None, local_port=9102)
    # An app already in 'stopped' should NOT be touched — only
    # running/starting/building apps are the self-update victims.
    _insert_app(config.db_path, "archive", "stopped", None, local_port=9103)

    # Simulate podman missing.  Patch at the consumption site in startup_mod
    # because it imported the name directly into its namespace.
    monkeypatch.setattr(startup_mod, "container_runtime_available", lambda: False)

    # Trap any call to start_app_process — the whole point is that we
    # must NOT try to rebuild when podman is unusable.
    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("start_app_process must not be called when podman is missing")

    monkeypatch.setattr(startup_mod, "start_app_process", boom)

    startup_mod.check_app_status(config)

    db = sqlite3.connect(config.db_path)
    try:
        rows = {row[0]: row for row in db.execute("SELECT name, status, error_message, container_id FROM apps")}
    finally:
        db.close()

    # All three affected apps (running, starting, building) must have
    # been flipped to error AND had their container_id cleared — pin
    # this explicitly for every row so a regression that only cleared
    # some statuses is caught.
    for name in ("notes", "wiki", "blog"):
        status, err, cid = rows[name][1:]
        assert status == "error", f"{name}: expected status=error, got {status}"
        assert err == CONTAINER_RUNTIME_MISSING_ERROR, f"{name}: wrong error_message"
        assert cid is None, f"{name}: container_id should be cleared, got {cid}"

    # A stopped app stays stopped — its error_message stays whatever
    # it was (None here) rather than getting stomped.
    assert rows["archive"][1] == "stopped"
    assert rows["archive"][2] is None


def test_check_app_status_podman_missing_no_running_apps_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh install / dev mode with no apps must not error when podman
    isn't available; it should just log and return."""
    config = _make_test_config(tmp_path, port=18501)
    _init_schema(config.db_path)

    monkeypatch.setattr(startup_mod, "container_runtime_available", lambda: False)

    # Does not raise.  No apps to update so rowcount=0, but that's fine.
    startup_mod.check_app_status(config)
