"""Tests for the ``/rename_app/<app>`` endpoint, focused on the three-tier directory rename (``app_data``, ``app_temp_data``, ``app_archive``) and the partial-failure rollback that keeps on-disk and DB state consistent when one tier fails (e.g. a transient JuiceFS mount drop)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


async def _post_rename(cfg, db_path: str, app_name: str, new_name: str) -> tuple[int, dict | None]:
    """Drive the unwrapped rename_app route via app.test_client().post (test_request_context doesn't populate request.form from data= in this Quart version)."""
    app = Quart(__name__)
    app.config["DB_PATH"] = db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    app.add_url_rule(
        f"/rename_app/{app_name}",
        view_func=apps_routes.rename_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
        defaults={"app_name": app_name},
    )
    client = app.test_client()
    with mock.patch.object(apps_routes, "stop_app_process"):
        response = await client.post(f"/rename_app/{app_name}", form={"name": new_name})
    try:
        payload = await response.get_json()
    except Exception:
        payload = None
    return response.status_code, payload


def _seed_app_row(db_path: str, name: str, port: int = 19500, status: str = "stopped") -> None:
    """Insert a minimal apps row and flip archive backend off the v7 'disabled' default into 'local' so the rename route's archive-health gate doesn't bounce the request with 503."""
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status)
               VALUES (?, '1.0', ?, ?, ?)""",
            (name, f"/tmp/repo/{name}", port, status),
        )
        db.execute("UPDATE archive_backend SET backend='local' WHERE id=1")
        db.commit()
    finally:
        db.close()


def _tier_parents(cfg) -> dict[str, Path]:
    """Single source of truth mapping tier name -> host-side parent dir; used by both setup and assertion helpers so they can't drift."""
    return {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
        "app_archive": Path(cfg.app_archive_dir),
    }


def _make_per_app_dirs(cfg, app_name: str, tiers: list[str]) -> dict[str, Path]:
    """Pre-create per-app subdirs under each named tier with a sentinel file so we can verify the rename actually moved content rather than creating an empty new dir."""
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
    """A rename must move app_data, app_temp_data, AND app_archive subdirectories; forgetting the archive tier would orphan its contents under the old name."""
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    assert status == 200, payload

    parents = _tier_parents(cfg)
    for tier, parent in parents.items():
        assert not (parent / "old-name").exists(), tier
        assert (parent / "new-name" / "sentinel.txt").read_text() == tier


@pytest.mark.asyncio
async def test_rename_skips_missing_tier_without_error(tmp_path: Path) -> None:
    """An app that never opted into app_archive has no subdir under the archive tier; rename_app must skip it cleanly rather than fail on a missing source."""
    cfg = _make_test_config(tmp_path, port=20201)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data"])

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    assert status == 200, payload
    assert not (Path(cfg.app_archive_dir) / "new-name").exists()


@pytest.mark.asyncio
async def test_rename_rollback_on_archive_failure(tmp_path: Path) -> None:
    """If the archive-tier rename fails partway through (e.g. JuiceFS mount transiently unhealthy), the previously-renamed app_data and app_temp_data dirs must be rolled back so on-disk state matches the unchanged DB rows."""
    cfg = _make_test_config(tmp_path, port=20202)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(cfg.app_archive_dir)

    def flaky_rename(src: str, dst: str) -> None:
        if os.path.realpath(os.path.dirname(src)) == archive_root:
            raise OSError(28, "simulated transient mount failure")
        real_rename(src, dst)

    with mock.patch("os.rename", side_effect=flaky_rename):
        status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")

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
async def test_rename_refuses_when_archive_parent_missing(tmp_path: Path) -> None:
    """If the archive backend's parent dir is missing (JuiceFS mount transiently dead), rename_app must refuse at the precheck rather than silently rename other tiers and orphan the archive subdir under the old name."""
    cfg = _make_test_config(tmp_path, port=20299)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    shutil.rmtree(cfg.app_archive_dir)

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    assert status == 503, payload
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
    """If a rollback rename also fails, the endpoint must still return 500 surfacing the original forward-rename failure (not the rollback failure), continue rolling back the other renamed tiers, and restore the DB status field."""
    cfg = _make_test_config(tmp_path, port=20203)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name", status="running")
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
        status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")

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
