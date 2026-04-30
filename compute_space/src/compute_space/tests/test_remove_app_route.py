"""Tests for the /remove_app/<name> Quart route."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


async def _call_remove(cfg, app_name: str, *, keep_data: bool = False):
    # Bypass @login_required by calling __wrapped__ directly — same
    # pattern as test_app_status.py.
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    form_data = {"keep_data": "1"} if keep_data else {}
    async with app.app_context(), app.test_request_context(f"/remove_app/{app_name}", method="POST", form=form_data):
        view = apps_routes.remove_app.__wrapped__  # type: ignore[attr-defined]
        response = await view(app_name)
        if isinstance(response, tuple):
            resp_obj, status = response[0], response[1]
        else:
            resp_obj, status = response, response.status_code
        payload = await resp_obj.get_json()
        return status, payload


def _seed_app(db_path: str, name: str, status: str = "running") -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status) VALUES (?, '1.0', '/r', 19500, ?)",
            (name, status),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_remove_returns_202_and_marks_removing(tmp_path: Path) -> None:
    """Happy path: row flips to 'removing' synchronously, worker is
    spawned, response is 202."""
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status, payload = await _call_remove(cfg, "myapp", keep_data=False)

    assert status == 202
    assert payload == {"ok": True}
    Thread.assert_called_once()
    # Constructing the Thread isn't enough — the route must call .start().
    Thread.return_value.start.assert_called_once()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE name = 'myapp'").fetchone()
    finally:
        db.close()
    assert row == ("removing",)


@pytest.mark.asyncio
async def test_remove_404_when_app_missing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))

    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status, payload = await _call_remove(cfg, "ghost")

    assert status == 404
    assert "not found" in (payload.get("error") or "").lower()
    Thread.assert_not_called()


@pytest.mark.asyncio
async def test_remove_rolls_back_if_thread_spawn_fails(tmp_path: Path) -> None:
    """If Thread.start() raises (resource exhaustion), the route must
    flip the row to 'error' so the operator can retry from the
    dashboard. Otherwise the row sits stuck in 'removing' and every
    retry hits the already_removing short-circuit.
    """
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp")

    failing_thread = MagicMock()
    failing_thread.return_value.start.side_effect = RuntimeError("can't start new thread")

    with patch("compute_space.web.routes.api.apps.threading.Thread", failing_thread):
        status, payload = await _call_remove(cfg, "myapp")

    assert status == 503
    assert "removal worker" in payload["error"].lower()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE name = 'myapp'").fetchone()
    finally:
        db.close()
    assert row[0] == "error"


@pytest.mark.asyncio
async def test_concurrent_removes_only_spawn_one_worker(tmp_path: Path) -> None:
    """Two POSTs racing on the same app: only one wins the atomic claim."""
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status1, payload1 = await _call_remove(cfg, "myapp")
        status2, payload2 = await _call_remove(cfg, "myapp")

    assert status1 == 202
    assert payload1 == {"ok": True}
    assert status2 == 202
    assert payload2.get("already_removing") is True
    assert Thread.call_count == 1


# Guards on sibling routes (stop, reload, rename) while a removal is in flight.


async def _call_route(cfg, view_name: str, app_name: str, *, form_data=None, method="POST"):
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    async with (
        app.app_context(),
        app.test_request_context(f"/{view_name}/{app_name}", method=method, form=form_data or {}),
    ):
        view = getattr(apps_routes, view_name).__wrapped__  # type: ignore[attr-defined]
        result = view(app_name)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, tuple):
            resp_obj, status = result[0], result[1]
        else:
            resp_obj, status = result, result.status_code
        payload = await resp_obj.get_json()
        return status, payload


@pytest.mark.asyncio
async def test_stop_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp", status="removing")

    status, payload = await _call_route(cfg, "stop_app", "myapp")
    assert status == 409
    assert "removed" in payload["error"].lower()


@pytest.mark.asyncio
async def test_reload_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp", status="removing")

    status, payload = await _call_route(cfg, "reload_app", "myapp")
    assert status == 409
    assert "removed" in payload["error"].lower()


@pytest.mark.asyncio
async def test_rename_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(_FakeApp(cfg.db_path))
    _seed_app(cfg.db_path, "myapp", status="removing")

    status, payload = await _call_route(cfg, "rename_app", "myapp", form_data={"name": "newname"})
    assert status == 409
    assert "removed" in payload["error"].lower()
