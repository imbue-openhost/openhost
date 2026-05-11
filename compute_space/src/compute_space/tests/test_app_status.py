"""Tests for the ``/api/app_status/<app_name>`` endpoint's error-kind logic.

In particular the legacy ``[CACHE_CORRUPT]`` marker still needs to map to
``error_kind = "build_cache_corrupt"`` so the dashboard's 'drop cache
and rebuild' toast keeps firing against rows whose ``error_message``
column was written before the marker rename.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
from compute_space.config import get_config
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


async def _app_status_response(tmp_path: Path, *, error_message: str, port: int) -> tuple[int, dict]:
    """Drive /api/app_status/<name> end-to-end against a real Quart app
    with a real on-disk sqlite schema.  Returns (status_code, payload)."""
    cfg = _make_test_config(tmp_path, port=port)
    init_db(_FakeApp(cfg.db_path))

    # Insert one app row with the error_message of interest.
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

    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    # apps_routes uses a login_required middleware; bypass it here by
    # calling the unwrapped view function directly.
    async with app.app_context(), app.test_request_context(f"/api/app_status/{app_id}"):
        response = apps_routes.app_status.__wrapped__(app_id)  # type: ignore[attr-defined]
        payload = await response.get_json()
        status = response.status_code
    return status, payload


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
    """Rows whose error_message was written before the marker rename
    must still trigger the 'drop cache' remediation toast in the UI."""
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
    """Make sure the module under test is still importing BUILD_CACHE_CORRUPT_MARKER
    the way app_status expects; a rename in core/containers.py that
    forgot the route handler would otherwise pass all other checks."""
    assert apps_routes.BUILD_CACHE_CORRUPT_MARKER == BUILD_CACHE_CORRUPT_MARKER


def test_get_config_is_present_for_router_tests() -> None:
    """Sanity: get_config() shouldn't raise at import time."""
    assert callable(get_config)
