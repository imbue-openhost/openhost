"""Tests for /api/rename_app's post-rename error handling.

When an app is renamed while it was running, the handler stops the old
container, renames the row + related tables, and then calls
``start_app_process`` with the new name.  If the restart fails (build
error, missing podman, timeout, …), the rename has already committed —
the handler must therefore catch the failure and persist it to the app
row rather than letting it bubble up as a 500, because otherwise the
dashboard sees "500 Internal Server Error" with no app-level context.

These tests mock out the container-runtime side of things so they can
run without a live podman daemon.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


def _insert_running_app(db_path: str, name: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status, container_id)
               VALUES (?, '1.0', '/repo/notes', 9100, 'running', 'cid-1')""",
            (name,),
        )
        db.commit()
    finally:
        db.close()


def _read_app_row(db_path: str, name: str) -> sqlite3.Row | None:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM apps WHERE name = ?", (name,)).fetchone()
    finally:
        db.close()


async def _invoke_rename(
    tmp_path: Path,
    *,
    old_name: str,
    new_name: str,
    port: int,
    start_app_process_side_effect: Exception | None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, dict, sqlite3.Row | None]:
    """Drive /api/rename_app/<old>.  Returns (status, payload, new-row-or-None)."""
    cfg = _make_test_config(tmp_path, port=port)
    init_db(_FakeApp(cfg.db_path))
    _insert_running_app(cfg.db_path, old_name)

    # Neutralise the side effects that touch podman / filesystem:
    # - stop_app_process: uses podman
    # - start_app_process: what we're testing — inject the side effect.
    monkeypatch.setattr(apps_routes, "stop_app_process", lambda _row: None)
    if start_app_process_side_effect is None:

        def fake_start(_name, _db, _config):  # type: ignore[no-untyped-def]
            return None

    else:

        def fake_start(_name, _db, _config):  # type: ignore[no-untyped-def]
            raise start_app_process_side_effect

    monkeypatch.setattr(apps_routes, "start_app_process", fake_start)

    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    async with (
        app.app_context(),
        app.test_request_context(
            f"/api/rename_app/{old_name}",
            method="POST",
            # rename_app reads form-encoded "name" field; pass it that way.
            form={"name": new_name},
        ),
    ):
        result = await apps_routes.rename_app.__wrapped__(old_name)  # type: ignore[attr-defined]

    # The success path returns a Response; error paths can return tuples.
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, result.status_code
    payload = await response.get_json()
    row = _read_app_row(cfg.db_path, new_name)
    return status, payload, row


@pytest.mark.asyncio
async def test_rename_success_with_restart_succeeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: if start_app_process succeeds, the rename returns 200
    and the new row exists with no error message.  Guards against a
    regression where the error-handling block accidentally swallows
    success too."""
    status, payload, row = await _invoke_rename(
        tmp_path,
        old_name="notes",
        new_name="notes2",
        port=20200,
        start_app_process_side_effect=None,
        monkeypatch=monkeypatch,
    )
    assert status == 200
    assert payload == {"ok": True, "name": "notes2"}
    assert row is not None
    assert row["error_message"] is None


@pytest.mark.asyncio
async def test_rename_persists_runtime_error_from_start_app_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If start_app_process raises RuntimeError (build failure), the
    rename has already committed, so we must not 500 — the handler
    records the error on the (new-name) row and returns 200."""
    status, payload, row = await _invoke_rename(
        tmp_path,
        old_name="notes",
        new_name="notes2",
        port=20201,
        start_app_process_side_effect=RuntimeError("Container build failed: boom"),
        monkeypatch=monkeypatch,
    )
    assert status == 200
    assert payload == {"ok": True, "name": "notes2"}
    assert row is not None
    assert row["status"] == "error"
    assert "Container build failed: boom" in row["error_message"]


@pytest.mark.asyncio
async def test_rename_persists_value_error_from_start_app_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ValueError path (manifest validation failing at restart)."""
    status, _payload, row = await _invoke_rename(
        tmp_path,
        old_name="notes",
        new_name="notes2",
        port=20202,
        start_app_process_side_effect=ValueError("manifest rejected: unsafe capability"),
        monkeypatch=monkeypatch,
    )
    assert status == 200
    assert row is not None
    assert row["status"] == "error"
    assert "manifest rejected" in row["error_message"]


@pytest.mark.asyncio
async def test_rename_persists_subprocess_timeout_from_start_app_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build-timeout path: core.containers.build_image re-raises
    subprocess.TimeoutExpired.  Without the explicit catch this would
    bubble out as a 500 while the rename has already committed, leaving
    the app stuck in 'stopped' with no error context."""
    status, _payload, row = await _invoke_rename(
        tmp_path,
        old_name="notes",
        new_name="notes2",
        port=20203,
        start_app_process_side_effect=subprocess.TimeoutExpired(cmd=["podman", "build"], timeout=300),
        monkeypatch=monkeypatch,
    )
    assert status == 200
    assert row is not None
    assert row["status"] == "error"
    # The TimeoutExpired.str() includes the command and timeout.
    assert "podman" in row["error_message"] or "300" in row["error_message"]


@pytest.mark.asyncio
async def test_rename_persists_oserror_from_start_app_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError path: mirrors what FileNotFoundError('podman') looks like
    on a Docker-era instance that just self-updated.  The rename must
    not 500 in that case; the error is persisted to the app row."""
    status, _payload, row = await _invoke_rename(
        tmp_path,
        old_name="notes",
        new_name="notes2",
        port=20204,
        start_app_process_side_effect=FileNotFoundError(2, "No such file or directory: 'podman'"),
        monkeypatch=monkeypatch,
    )
    assert status == 200
    assert row is not None
    assert row["status"] == "error"
    assert "podman" in row["error_message"]
