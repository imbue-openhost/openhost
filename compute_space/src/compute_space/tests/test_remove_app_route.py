"""Tests for the /remove_app/{app_id} Litestar route."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.config import set_active_config
from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db

from .conftest import _make_test_config


async def _user_stub() -> dict[str, str]:
    return {"sub": "owner", "username": "owner"}


def _make_app(cfg, route_handlers) -> Litestar:
    set_active_config(cfg)
    return Litestar(
        route_handlers=route_handlers,
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
        logging_config=None,
    )


async def _call_remove(cfg, app_id: str, *, keep_data: bool = False):
    app = _make_app(cfg, [apps_routes.remove_app])
    form_data = {"keep_data": "1"} if keep_data else {}
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(f"/remove_app/{app_id}", data=form_data)
    payload = resp.json() if resp.content else None
    return resp.status_code, payload


def _seed_app(db_path: str, name: str, status: str = "running") -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, '1.0', '/r', 19500, ?)",
            (app_id, name, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


@pytest.mark.asyncio
async def test_remove_returns_202_and_marks_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status, payload = await _call_remove(cfg, app_id, keep_data=False)

    assert status == 202
    assert payload == {"ok": True}
    Thread.assert_called_once()
    Thread.return_value.start.assert_called_once()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row == ("removing",)


@pytest.mark.asyncio
async def test_remove_404_when_app_missing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status, payload = await _call_remove(cfg, new_app_id())
    assert status == 404
    assert "not found" in (payload.get("error") or "").lower()
    Thread.assert_not_called()


@pytest.mark.asyncio
async def test_remove_rolls_back_if_thread_spawn_fails(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp")

    failing_thread = MagicMock()
    failing_thread.return_value.start.side_effect = RuntimeError("can't start new thread")

    with patch("compute_space.web.routes.api.apps.threading.Thread", failing_thread):
        status, payload = await _call_remove(cfg, app_id)

    assert status == 503
    assert "removal worker" in payload["error"].lower()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row[0] == "error"


@pytest.mark.asyncio
async def test_concurrent_removes_only_spawn_one_worker(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.threading.Thread") as Thread:
        status1, payload1 = await _call_remove(cfg, app_id)
        status2, payload2 = await _call_remove(cfg, app_id)

    assert status1 == 202
    assert payload1 == {"ok": True}
    assert status2 == 202
    assert payload2.get("already_removing") is True
    assert Thread.call_count == 1


# Guards on sibling routes (stop, reload, rename) while a removal is in flight.


async def _call_route_form(cfg, view, app_id: str, *, form_data=None):
    app = _make_app(cfg, [view])
    async with AsyncTestClient(app=app) as client:
        if form_data is None and view is apps_routes.stop_app:
            resp = await client.post(f"/stop_app/{app_id}")
        elif view is apps_routes.reload_app:
            resp = await client.post(f"/reload_app/{app_id}", data=form_data or {})
        elif view is apps_routes.rename_app:
            resp = await client.post(f"/rename_app/{app_id}", data=form_data or {})
        else:
            raise NotImplementedError(view)
    payload = resp.json() if resp.content else None
    return resp.status_code, payload


@pytest.mark.asyncio
async def test_stop_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    status, payload = await _call_route_form(cfg, apps_routes.stop_app, app_id)
    assert status == 409
    assert "removed" in payload["error"].lower()


@pytest.mark.asyncio
async def test_reload_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    status, payload = await _call_route_form(cfg, apps_routes.reload_app, app_id)
    assert status == 409
    assert "removed" in payload["error"].lower()


@pytest.mark.asyncio
async def test_rename_app_refuses_when_removing(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    status, payload = await _call_route_form(cfg, apps_routes.rename_app, app_id, form_data={"name": "newname"})
    assert status == 409
    assert "removed" in payload["error"].lower()
