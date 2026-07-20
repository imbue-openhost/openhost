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


def test_fresh_db_seeds_guard_enabled_at_default(cfg: Any) -> None:
    # The guard ships enabled with the 1500 MB default on a fresh DB.
    s = _read(cfg)
    assert s.enabled is True
    assert s.min_free_mb == 1500


def test_seed_default_matches_constant(cfg: Any) -> None:
    # The DB seed (from the v0012 migration / schema.sql) must match the
    # canonical DEFAULT_GUARD_MIN_FREE_MB constant, so the SQL literal and the
    # Python constant cannot silently drift.
    assert _read(cfg).min_free_mb == storage.DEFAULT_GUARD_MIN_FREE_MB


def test_status_reports_guard_settings(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.get("/api/storage-status", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["guard_enabled"] is True
    assert body["guard_min_free_mb"] == 1500
    assert body["storage_min_free_bytes"] == 1500 * 1024 * 1024


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


def test_enable_with_zero_threshold_rejected(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    # Set a known state first so we can assert the rejected request is a no-op.
    client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 900}, cookies=cookies)
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 0}, cookies=cookies)
    assert resp.status_code == 400
    assert "greater than 0" in resp.json()["error"]
    # Unchanged from the known state.
    s = _read(cfg)
    assert s.enabled is False
    assert s.min_free_mb == 900


def test_negative_threshold_rejected(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 900}, cookies=cookies)
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": -5}, cookies=cookies)
    assert resp.status_code == 400
    # Unchanged from the known state.
    s = _read(cfg)
    assert s.min_free_mb == 900
    assert s.enabled is True


def test_disable_with_zero_threshold_allowed(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    # Turning the guard off with a 0 threshold is the normal "off" state.
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 0}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["guard_enabled"] is False


# --- pause interaction ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pause() -> Iterator[None]:
    """The guard pause is process-global in-memory state; reset it around each
    test so pause assertions don't leak between tests."""
    storage.set_guard_paused(False)
    try:
        yield
    finally:
        storage.set_guard_paused(False)


def test_fresh_enable_clears_pause(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    # Start disabled, with the guard paused.
    client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 1000}, cookies=cookies)
    storage.set_guard_paused(True)
    # Enabling the guard (disabled -> enabled) resumes enforcement.
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000}, cookies=cookies)
    assert resp.status_code == 200
    assert storage.is_guard_paused() is False


def test_resave_of_enabled_guard_preserves_pause(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """Re-saving an already-enabled guard (e.g. changing only the threshold)
    must NOT clear an owner-set pause — otherwise adjusting settings while a
    cleanup app runs would let the guard stop that app."""
    client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000}, cookies=cookies)
    # Owner pauses the guard to run a cleanup app while disk is low.
    storage.set_guard_paused(True)
    # Owner then adjusts the threshold; the guard stays enabled.
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 2000}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["guard_min_free_mb"] == 2000
    # The pause must survive.
    assert storage.is_guard_paused() is True


def test_disable_does_not_touch_pause(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000}, cookies=cookies)
    storage.set_guard_paused(True)
    resp = client.post("/api/storage-settings", json={"enabled": False, "min_free_mb": 1000}, cookies=cookies)
    assert resp.status_code == 200
    # Disabling doesn't clear the pause flag (it's irrelevant while disabled,
    # but we must not silently mutate it).
    assert storage.is_guard_paused() is True


# --- auth -----------------------------------------------------------------


def test_requires_auth(client: TestClient[Litestar]) -> None:
    resp = client.post("/api/storage-settings", json={"enabled": True, "min_free_mb": 1000})
    assert resp.status_code in (401, 403)
