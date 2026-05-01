"""Tests for the ``/rename_app/<app>`` endpoint, focused on the
three-tier directory rename and the partial-failure rollback.

The endpoint renames per-app subdirectories under three storage tiers
(``app_data``, ``app_temp_data``, ``app_archive``) and then updates a
small set of related DB rows.  When ``app_archive_dir`` resolves to a
JuiceFS mount, a transient mount drop or permissions blip during the
rename loop would otherwise leave the on-disk and DB state in an
inconsistent state — the archive subdir abandoned under the old name
while the rest of the world refers to the new one.

These tests exercise the happy path plus the rollback, both directly
against the route function (no live router process) so they stay
fast and don't depend on podman.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
from compute_space.db.connection import init_db

from .conftest import _FakeApp, _make_test_config


async def _post_rename(
    cfg, db_path: str, app_name: str, new_name: str
) -> tuple[int, dict | None]:
    """Drive the unwrapped rename_app route under a Quart context.

    Uses ``app.test_client().post`` rather than ``test_request_context``
    because the latter doesn't populate ``request.form`` from the
    ``data=`` kwarg in this version of Quart, leaving the route to
    400 with "Name is required" before we ever get to the rename
    logic we're trying to exercise.
    """
    app = Quart(__name__)
    app.config["DB_PATH"] = db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    # Register the unwrapped view directly so the @login_required
    # decorator doesn't bounce the request to /login.
    app.add_url_rule(
        f"/rename_app/{app_name}",
        view_func=apps_routes.rename_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
        defaults={"app_name": app_name},
    )
    client = app.test_client()
    with mock.patch.object(apps_routes, "stop_app_process"):
        response = await client.post(
            f"/rename_app/{app_name}", form={"name": new_name}
        )
    try:
        payload = await response.get_json()
    except Exception:
        payload = None
    return response.status_code, payload


def _seed_app_row(
    db_path: str, name: str, port: int = 19500, status: str = "stopped"
) -> None:
    """Insert a minimal apps row that rename_app can target."""
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status)
               VALUES (?, '1.0', ?, ?, ?)""",
            (name, f"/tmp/repo/{name}", port, status),
        )
        db.commit()
    finally:
        db.close()


def _tier_parents(cfg) -> dict[str, Path]:
    """Single source of truth mapping tier name -> host-side parent dir.

    Used both by the setup helper that creates the per-app subdirs
    and by the assertions that verify their post-rename location, so
    the two can't drift.
    """
    return {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
        "app_archive": Path(cfg.app_archive_dir),
    }


def _make_per_app_dirs(cfg, app_name: str, tiers: list[str]) -> dict[str, Path]:
    """Pre-create the per-app subdirs under each named tier and drop a
    sentinel file in each so we can verify the rename moved the content
    rather than just creating an empty new dir.
    """
    parents = _tier_parents(cfg)
    out: dict[str, Path] = {}
    for tier in tiers:
        d = parents[tier] / app_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sentinel.txt").write_text(tier)
        out[tier] = d
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_renames_all_three_tiers(tmp_path: Path) -> None:
    """A rename must move app_data, app_temp_data, AND app_archive
    subdirectories.  Forgetting the archive tier would orphan its
    contents under the old name.
    """
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    assert status == 200, payload

    # Old subdirs gone, new subdirs present, sentinel content preserved
    # so we know we actually moved (not recreated) each tier.
    parents = _tier_parents(cfg)
    for tier, parent in parents.items():
        assert not (parent / "old-name").exists(), tier
        assert (parent / "new-name" / "sentinel.txt").read_text() == tier


@pytest.mark.asyncio
async def test_rename_skips_missing_tier_without_error(tmp_path: Path) -> None:
    """An app that never opted into app_archive has no subdir under the
    archive tier.  rename_app must skip it cleanly, not fail because
    the source dir is missing.
    """
    cfg = _make_test_config(tmp_path, port=20201)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name")
    # Only app_data + app_temp_data exist; app_archive subdir absent.
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data"])

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    assert status == 200, payload
    # And no spurious archive subdir got created on the way through.
    assert not (Path(cfg.app_archive_dir) / "new-name").exists()


# ---------------------------------------------------------------------------
# Partial-failure rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_rollback_on_archive_failure(tmp_path: Path) -> None:
    """If the archive-tier rename fails partway through (e.g. the
    JuiceFS mount went transiently unhealthy), the previously-renamed
    app_data and app_temp_data directories must be rolled back so the
    operator-visible state matches the DB (which still has the old
    name on disk and in the rows).
    """
    cfg = _make_test_config(tmp_path, port=20202)
    init_db(_FakeApp(cfg.db_path))
    # Seed the row as ``running`` so the rollback assertion below
    # actually catches the regression where the status field stays
    # ``stopped`` after the failed rename.
    _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(cfg.app_archive_dir)

    def flaky_rename(src: str, dst: str) -> None:
        # Fail the archive-tier rename specifically; let the others go.
        if os.path.realpath(os.path.dirname(src)) == archive_root:
            raise OSError(28, "simulated transient mount failure")
        real_rename(src, dst)

    with mock.patch("os.rename", side_effect=flaky_rename):
        status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")

    # Endpoint must surface the failure rather than swallowing it.
    assert status == 500, payload

    # All three old-name subdirs must be restored; no new-name subdirs
    # in any tier.  Without the rollback, app_data/new-name and
    # app_temp_data/new-name would have leaked through.
    for tier, parent in _tier_parents(cfg).items():
        assert (parent / "old-name").exists(), f"{tier} not rolled back"
        assert not (parent / "new-name").exists(), f"{tier} leaked partial rename"

    # The DB row also stays under the old name AND keeps its prior
    # status — without restoring status, an app that was running
    # before the rename would be permanently stuck as ``stopped``
    # in the DB even though the on-disk state was rolled back.
    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows


@pytest.mark.asyncio
async def test_rename_refuses_when_archive_parent_missing(tmp_path: Path) -> None:
    """If the archive backend's parent dir is missing — the symptom
    of a JuiceFS mount that's transiently dead — rename_app must
    refuse rather than silently renaming the other tiers and
    orphaning the archive subdir under the old name.
    """
    cfg = _make_test_config(tmp_path, port=20299)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    # Simulate the mount drop by removing the archive parent itself
    # (this models JuiceFS being unhealthy — the mount point exists
    # in the parent FS but isn't a directory anymore).
    import shutil as _shutil  # local import: not used in main scope

    _shutil.rmtree(cfg.app_archive_dir)

    status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")
    # 503 = the route layer's archive_backend.is_archive_dir_healthy
    # check rejected the rename because the local-default archive
    # parent is missing.  An earlier 500 from _rename_app_storage_dirs
    # would also have been valid; the route-layer 503 is preferred
    # because it surfaces the operator-actionable cause more directly.
    assert status == 503, payload
    assert "Archive backend" in (payload or {}).get("error", ""), payload

    # No tier got renamed because we refused at the precheck.
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
    """If a rollback rename ALSO fails (e.g. the underlying filesystem
    is gone for both forward and reverse), the endpoint must still:

    - Return 500 surfacing the *original* forward-rename failure (not
      the rollback failure), so operators know the trigger.
    - Continue rolling back the OTHER renamed tiers — a single bad
      tier shouldn't abandon the rest as new-name when the operator-
      visible state needs to be old-name.
    - Restore the DB status field, since the on-disk + DB-status
      consistency invariant the route promises is the same regardless
      of how partial the on-disk rollback turned out.
    """
    cfg = _make_test_config(tmp_path, port=20203)
    init_db(_FakeApp(cfg.db_path))
    _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(cfg.app_archive_dir)
    app_temp_root = os.path.realpath(
        str(Path(cfg.temporary_data_dir) / "app_temp_data")
    )

    def flaky_rename(src: str, dst: str) -> None:
        parent = os.path.realpath(os.path.dirname(src))
        # Forward rename of the archive tier fails (the trigger).
        if parent == archive_root and os.path.basename(src) == "old-name":
            raise OSError(28, "simulated transient archive mount failure")
        # Rollback rename of app_temp_data also fails (the wrinkle).
        if parent == app_temp_root and os.path.basename(src) == "new-name":
            raise OSError(5, "simulated rollback rename failure")
        real_rename(src, dst)

    with mock.patch("os.rename", side_effect=flaky_rename):
        status, payload = await _post_rename(cfg, cfg.db_path, "old-name", "new-name")

    assert status == 500, payload
    # The error surfaced must be the original archive-rename failure,
    # not the rollback failure — operators need the trigger, not the
    # downstream symptom.
    assert "transient archive mount failure" in (payload or {}).get("error", ""), payload

    # app_data successfully rolled back to old-name.  app_temp_data
    # is the wedged tier — its forward rename succeeded but its
    # rollback rename failed, so it sits at new-name.  This documents
    # the limitation: the route is best-effort, and the operator log
    # is the recovery path for cases where rollback also fails.
    parents = _tier_parents(cfg)
    assert (parents["app_data"] / "old-name").exists()
    assert not (parents["app_data"] / "new-name").exists()
    assert (parents["app_archive"] / "old-name").exists()
    assert not (parents["app_archive"] / "new-name").exists()
    # The wedged tier — explicitly assert this rather than glossing
    # over it, so a future refactor that tries harder to rollback
    # has a clear signal where to update the test.
    assert (parents["app_temp_data"] / "new-name").exists()
    assert not (parents["app_temp_data"] / "old-name").exists()

    # The DB-status restore path runs regardless of partial rollback,
    # because the operator's dashboard is the only signal they have
    # without grepping logs.
    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows
