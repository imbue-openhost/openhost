"""Tests for the ``/rename_app/{app_id}`` endpoint."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest import mock

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


def _make_app(cfg) -> Litestar:
    set_active_config(cfg)
    return Litestar(
        route_handlers=[apps_routes.rename_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )


async def _post_rename(cfg, db_path: str, app_id: str, new_name: str) -> tuple[int, dict | None]:
    app = _make_app(cfg)
    with (
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=True),
    ):
        async with AsyncTestClient(app=app) as client:
            response = await client.post(f"/rename_app/{app_id}", data={"name": new_name})
    payload = response.json() if response.content else None
    return response.status_code, payload


def _seed_app_row(db_path: str, name: str, port: int = 19500, status: str = "stopped") -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, '1.0', ?, ?, ?)""",
            (app_id, name, f"/tmp/repo/{name}", port, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _tier_parents(cfg) -> dict[str, Path]:
    return {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
        "app_archive": Path(cfg.app_archive_dir),
    }


def _make_per_app_dirs(cfg, app_name: str, tiers: list[str]) -> dict[str, Path]:
    parents = _tier_parents(cfg)
    out: dict[str, Path] = {}
    for tier in tiers:
        d = parents[tier] / app_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sentinel.txt").write_text(tier)
        out[tier] = d
    return out


@pytest.mark.asyncio
async def test_rename_renames_all_three_tiers(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(cfg.db_path)
    app_id = _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    status, payload = await _post_rename(cfg, cfg.db_path, app_id, "new-name")
    assert status == 200, payload
    parents = _tier_parents(cfg)
    for tier, parent in parents.items():
        assert not (parent / "old-name").exists(), tier
        assert (parent / "new-name" / "sentinel.txt").read_text() == tier


@pytest.mark.asyncio
async def test_rename_skips_missing_tier_without_error(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20201)
    init_db(cfg.db_path)
    app_id = _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data"])

    status, payload = await _post_rename(cfg, cfg.db_path, app_id, "new-name")
    assert status == 200, payload
    assert not (Path(cfg.app_archive_dir) / "new-name").exists()


@pytest.mark.asyncio
async def test_rename_rollback_on_archive_failure(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20202)
    init_db(cfg.db_path)
    app_id = _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(cfg.app_archive_dir)

    def flaky_rename(src: str, dst: str) -> None:
        if os.path.realpath(os.path.dirname(src)) == archive_root:
            raise OSError(28, "simulated transient mount failure")
        real_rename(src, dst)

    with mock.patch("os.rename", side_effect=flaky_rename):
        status, payload = await _post_rename(cfg, cfg.db_path, app_id, "new-name")

    assert status == 500, payload

    for tier, parent in _tier_parents(cfg).items():
        assert (parent / "old-name").exists(), f"{tier} not rolled back"
        assert not (parent / "new-name").exists(), f"{tier} leaked partial rename"

    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows


@pytest.mark.asyncio
async def test_rename_refuses_archive_using_app_when_archive_unhealthy(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20299)
    init_db(cfg.db_path)

    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, '1.0', ?, ?, 'running', ?)""",
            (app_id, "old-name", "/tmp/repo/old-name", 19500, "[data]\napp_archive = true\n"),
        )
        db.commit()
    finally:
        db.close()
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    set_active_config(cfg)
    app = Litestar(
        route_handlers=[apps_routes.rename_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    with (
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=False),
    ):
        async with AsyncTestClient(app=app) as client:
            response = await client.post(f"/rename_app/{app_id}", data={"name": "new-name"})
    payload = response.json() if response.content else None
    assert response.status_code == 503, payload
    assert "Archive backend" in (payload or {}).get("error", ""), payload

    parents_present = {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
    }
    for tier, parent in parents_present.items():
        assert (parent / "old-name").exists(), tier
        assert not (parent / "new-name").exists(), tier


@pytest.mark.asyncio
async def test_rename_rollback_continues_when_a_rollback_rename_itself_fails(
    tmp_path: Path,
) -> None:
    cfg = _make_test_config(tmp_path, port=20203)
    init_db(cfg.db_path)
    app_id = _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(cfg.app_archive_dir)
    app_temp_root = os.path.realpath(str(Path(cfg.temporary_data_dir) / "app_temp_data"))

    def flaky_rename(src: str, dst: str) -> None:
        parent = os.path.realpath(os.path.dirname(src))
        if parent == archive_root and os.path.basename(src) == "old-name":
            raise OSError(28, "simulated transient archive mount failure")
        if parent == app_temp_root and os.path.basename(src) == "new-name":
            raise OSError(5, "simulated rollback rename failure")
        real_rename(src, dst)

    with mock.patch("os.rename", side_effect=flaky_rename):
        status, payload = await _post_rename(cfg, cfg.db_path, app_id, "new-name")

    assert status == 500, payload
    assert "transient archive mount failure" in (payload or {}).get("error", ""), payload

    parents = _tier_parents(cfg)
    assert (parents["app_data"] / "old-name").exists()
    assert not (parents["app_data"] / "new-name").exists()
    assert (parents["app_archive"] / "old-name").exists()
    assert not (parents["app_archive"] / "new-name").exists()
    assert (parents["app_temp_data"] / "new-name").exists()
    assert not (parents["app_temp_data"] / "old-name").exists()

    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows


@pytest.mark.asyncio
async def test_rename_non_archive_app_works_with_disabled_backend(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20305)
    init_db(cfg.db_path)

    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, '1.0', ?, ?, 'stopped', ?)""",
            (app_id, "plain", "/tmp/repo/plain", 19510, "[data]\napp_data = true\n"),
        )
        db.commit()
    finally:
        db.close()
    _make_per_app_dirs(cfg, "plain", ["app_data", "app_temp_data"])

    set_active_config(cfg)
    app = Litestar(
        route_handlers=[apps_routes.rename_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    with mock.patch.object(apps_routes, "stop_app_process"):
        async with AsyncTestClient(app=app) as client:
            response = await client.post(f"/rename_app/{app_id}", data={"name": "renamed"})
    payload = response.json() if response.content else None
    assert response.status_code == 200, payload
    assert payload["name"] == "renamed"
