"""Tests for the ``/api/storage-settings`` and ``/api/storage-status`` endpoints.

These cover the runtime-configurable storage guard: enabling/disabling it and
setting the minimum-free-MB threshold from the System page, persisted in the
``storage_settings`` table and applied without a restart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

import compute_space.core.storage as storage
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.system import system_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20500)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(system_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _read(cfg: Any) -> storage.StorageSettings:
    db = sqlite3.connect(cfg.db_path)
    try:
        return storage.read_storage_settings(db)
    finally:
        db.close()


# --- defaults -------------------------------------------------------------


def test_fresh_db_seeds_disabled_guard(cfg: Any) -> None:
    s = _read(cfg)
    assert s.enabled is False
    assert s.min_free_mb == 0


def test_status_reports_guard_settings(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.get("/api/storage-status", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["guard_enabled"] is False
    assert body["guard_min_free_mb"] == 0
    assert body["storage_min_free_bytes"] is None


# --- enabling / disabling -------------------------------------------------


def test_enable_guard_persists_and_reflects_in_status(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    resp = client.post(
        "/api/storage-settings",
        json={"enabled": True, "min_free_mb": 2048},
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["guard_enabled"] is True
    assert body["guard_min_free_mb"] == 2048

    # Persisted.
    s = _read(cfg)
    assert s.enabled is True
    assert s.min_free_mb == 2048

    # Reflected in status, including the derived byte threshold.
    status = client.get("/api/storage-status", cookies=cookies).json()
    assert status["guard_enabled"] is True
    assert status["guard_min_free_mb"] == 2048
    assert status["storage_min_free_bytes"] == 2048 * 1024 * 1024


def test_disable_guard(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000}, cookies=cookies)
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 1000}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["guard_enabled"] is False
    # Disabled means no effective threshold even though min_free_mb is retained.
    status = client.get("/api/storage-status", cookies=cookies).json()
    assert status["storage_min_free_bytes"] is None
    assert status["guard_min_free_mb"] == 1000


# --- validation -----------------------------------------------------------


def test_enable_with_zero_threshold_rejected(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 0}, cookies=cookies)
    assert resp.status_code == 400
    assert "greater than 0" in resp.json()["error"]
    # Unchanged.
    assert _read(cfg).enabled is False


def test_negative_threshold_rejected(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": -5}, cookies=cookies)
    assert resp.status_code == 400
    assert _read(cfg).min_free_mb == 0


def test_disable_with_zero_threshold_allowed(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    # Turning the guard off with a 0 threshold is the normal "off" state.
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 0}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["guard_enabled"] is False


# --- auth -----------------------------------------------------------------


def test_requires_auth(client: TestClient[Litestar]) -> None:
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000})
    assert resp.status_code in (401, 403)
