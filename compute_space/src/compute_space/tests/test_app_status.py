"""Tests for the ``/api/app_status/<app_id>`` endpoint's error-kind logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.config import get_config
from compute_space.config import set_active_config
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.db.connection import init_db

from .conftest import _make_test_config


async def _user_stub() -> dict[str, str]:
    return {"sub": "owner", "username": "owner"}


async def _app_status_response(tmp_path: Path, *, error_message: str, port: int) -> tuple[int, dict]:
    cfg = _make_test_config(tmp_path, port=port)
    init_db(cfg.db_path)
    set_active_config(cfg)

    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, error_message)
               VALUES (?, 'notes', '1.0', '/repo/notes', ?, 'error', ?)""",
            (app_id, port + 10, error_message),
        )
        db.commit()
    finally:
        db.close()

    app = Litestar(
        route_handlers=[apps_routes.app_status],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(f"/api/app_status/{app_id}")
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_current_marker_maps_to_build_cache_corrupt_error_kind(tmp_path: Path) -> None:
    status, payload = await _app_status_response(
        tmp_path,
        error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
        port=20100,
    )
    assert status == 200
    assert payload["error_kind"] == "build_cache_corrupt"
    assert payload["error"] == "Container build cache is corrupted."


@pytest.mark.asyncio
async def test_legacy_marker_still_maps_to_build_cache_corrupt_error_kind(tmp_path: Path) -> None:
    status, payload = await _app_status_response(
        tmp_path,
        error_message="[CACHE_CORRUPT] Docker build cache is corrupted.",
        port=20101,
    )
    assert status == 200
    assert payload["error_kind"] == "build_cache_corrupt"
    assert payload["error"] == "Container build cache is corrupted."


@pytest.mark.asyncio
async def test_unrelated_error_message_has_no_error_kind(tmp_path: Path) -> None:
    status, payload = await _app_status_response(
        tmp_path,
        error_message="App started but not responding to HTTP",
        port=20102,
    )
    assert status == 200
    assert payload["error_kind"] is None


def test_import_wiring() -> None:
    assert apps_routes.BUILD_CACHE_CORRUPT_MARKER == BUILD_CACHE_CORRUPT_MARKER


def test_get_config_is_present_for_router_tests() -> None:
    assert callable(get_config)
