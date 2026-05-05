"""Tests for the ``/api/storage/archive_backend`` endpoints.

Drives the routes through Quart's test client so the full
form-parsing + JSON serialisation paths are exercised.  The actual
JuiceFS subprocess work in ``switch_backend`` is mocked at the
core-module boundary so these tests stay fast and don't need a
real S3 bucket.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
import compute_space.web.routes.api.archive_backend as routes
from compute_space.core import archive_backend
from compute_space.core.manifest import AppManifest
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


def _make_app(cfg) -> Quart:  # noqa: ANN001
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    app.add_url_rule(
        "/api/storage/archive_backend",
        view_func=routes.get_archive_backend.__wrapped__,  # type: ignore[attr-defined]
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/storage/archive_backend",
        endpoint="post_archive_backend",
        view_func=routes.post_archive_backend.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/storage/archive_backend/test_connection",
        view_func=routes.test_connection.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    return app


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20400)


@pytest.fixture
def app(cfg):
    init_db(_FakeApp(cfg.db_path))
    yield _make_app(cfg)


@pytest.mark.asyncio
async def test_get_returns_seeded_disabled_state(app):
    """A fresh DB returns the seeded ``disabled`` row with no S3 fields set and ``archive_dir`` null; the v7 migration flipped the default from 'local' to 'disabled' so app_archive apps refuse to install on a brand-new zone until the operator picks a backend."""
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["backend"] == "disabled"
    assert body["state"] == "idle"
    assert body["s3_bucket"] is None
    assert body["archive_dir"] is None
    assert body["meta_dumps"] is None
    assert "s3_secret_access_key" not in body


@pytest.mark.asyncio
async def test_get_redacts_secret_when_s3(app):
    """In the s3 backend the access_key_id is visible (so the
    dashboard can display "currently using AKIA…") but the secret is
    never returned, even to authenticated requests."""
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIASOMETHING', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["s3_access_key_id"] == "AKIASOMETHING"
    assert "s3_secret_access_key" not in body
    assert body["s3_bucket"] == "b"


@pytest.mark.asyncio
async def test_get_surfaces_meta_db_path(app):
    """The dashboard renders ``meta_db_path`` (under ``juicefs/state/``) for both backends so the operator can pre-plan their backup story before flipping the switch."""
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert "meta_db_path" in body
    assert body["meta_db_path"].endswith("/juicefs/state/meta.db"), body["meta_db_path"]


@pytest.mark.asyncio
async def test_get_surfaces_meta_dumps_when_s3(app):
    """When backend=s3, GET runs ``list_meta_dumps`` and surfaces the
    summary so the dashboard can render "Last metadata dump: <ts>".
    Mocked at the core helper level to avoid hitting S3.
    """
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_prefix='zone-a', s3_access_key_id='AKIA', "
            "s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    summary = archive_backend.MetaDumpSummary(
        count=42,
        latest_at="2026-05-01T18:00:00Z",
        latest_key="zone-a/meta/dump-2026-05-01-180000.json.gz",
    )
    client = app.test_client()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=summary):
        resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["meta_dumps"] == {
        "count": 42,
        "latest_at": "2026-05-01T18:00:00Z",
        "latest_key": "zone-a/meta/dump-2026-05-01-180000.json.gz",
    }


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_non_s3_backends(app):
    """Backends other than s3 (disabled, local) have ``meta_dumps`` set to ``None`` so the dashboard renders nothing rather than a misleading "0 dumps" message; covers both disabled and local in one test."""
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "disabled"
    assert body["meta_dumps"] is None

    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute("UPDATE archive_backend SET backend='local'")
        db.commit()
    finally:
        db.close()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "local"
    assert body["meta_dumps"] is None


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_s3_list_failure(app):
    """When ``list_meta_dumps`` returns None (S3 unreachable, etc.) the response surfaces ``meta_dumps: null`` so the dashboard distinguishes "status unavailable" from "no dumps yet"."""
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    client = app.test_client()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=None):
        resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["meta_dumps"] is None


@pytest.mark.asyncio
async def test_post_rejects_unknown_backend(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "blob", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    assert "local" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_rejects_disabled_as_target(app):
    """``backend=disabled`` is rejected (400) because going back to disabled would orphan the on-disk / in-bucket archive bytes with no openhost-side handle to recover them."""
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "disabled", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert "local" in body["error"] or "s3" in body["error"], body


@pytest.mark.asyncio
async def test_post_requires_confirm_data_loss(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "s3", "s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
    )
    assert resp.status_code == 400
    assert "confirm_data_loss" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_s3_requires_creds(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "s3", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    assert "Missing required fields" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_rejects_invalid_s3_prefix(app):
    """Malformed prefix (path traversal, whitespace, NUL, multi-segment, uppercase, underscore, too short, leading/trailing dash, dot) is rejected at the route layer because it's used directly as the JuiceFS volume name (regex ``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``)."""
    client = app.test_client()
    bads = (
        "../etc",
        "with space",
        "embedded\x00null",
        "a/b",
        "UPPER",
        "under_score",
        "ab",
        "-leading-dash",
        "trailing-dash-",
        "with.dot",
    )
    for bad in bads:
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "s3_prefix": bad,
            },
        )
        body = await resp.get_json()
        assert resp.status_code == 400, (bad, body)
        assert "s3_prefix" in body["error"], (bad, body)


@pytest.mark.asyncio
async def test_post_rejects_when_already_switching(app):
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute("UPDATE archive_backend SET state='switching'")
        db.commit()
    finally:
        db.close()
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={
            "backend": "s3",
            "confirm_data_loss": "true",
            "s3_bucket": "b",
            "s3_access_key_id": "a",
            "s3_secret_access_key": "s",
        },
    )
    assert resp.status_code == 409
    assert "already in progress" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_local_to_s3_returns_202_and_runs_switch(app, cfg):
    """Switching local -> s3 returns 202 with state=switching; the background thread eventually flips the DB row to s3/idle."""
    juicefs_mount = archive_backend.juicefs_mount_dir(cfg)
    Path(juicefs_mount).mkdir(parents=True, exist_ok=True)

    client = app.test_client()
    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount"),
    ):
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "mybucket",
                "s3_region": "us-east-1",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "hunter2",
            },
        )
        assert resp.status_code == 202
        body = await resp.get_json()
        assert body["state"] == "switching"

        deadline = time.time() + 5
        while time.time() < deadline:
            db = sqlite3.connect(cfg.db_path)
            try:
                row = db.execute("SELECT backend, state FROM archive_backend WHERE id=1").fetchone()
            finally:
                db.close()
            if row[0] == "s3" and row[1] == "idle":
                break
            time.sleep(0.05)

    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["state"] == "idle"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body
    assert body["archive_dir"] == juicefs_mount


@pytest.mark.asyncio
async def test_post_local_to_s3_with_prefix_persists_prefix(app, cfg):
    """A non-empty s3_prefix round-trips cleanly: it becomes the JuiceFS volume name passed to format_volume (not a separate s3_prefix kwarg, since JuiceFS won't accept a path component on the bucket URL), is persisted as both ``s3_prefix`` and ``juicefs_volume_name`` in the DB row, and is surfaced on the next GET response in both fields."""
    juicefs_mount = archive_backend.juicefs_mount_dir(cfg)
    Path(juicefs_mount).mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def _capture_format(*args, **kwargs):
        captured.update(kwargs)
        if args:
            captured["_positional_count"] = len(args)

    client = app.test_client()
    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume", side_effect=_capture_format),
        mock.patch.object(archive_backend, "mount"),
    ):
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "imbue-openhost",
                "s3_region": "us-west-2",
                "s3_prefix": "andrew-3",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "hunter2",
            },
        )
        assert resp.status_code == 202

        deadline = time.time() + 5
        while time.time() < deadline:
            db = sqlite3.connect(cfg.db_path)
            try:
                row = db.execute("SELECT backend, state FROM archive_backend WHERE id=1").fetchone()
            finally:
                db.close()
            if row[0] == "s3" and row[1] == "idle":
                break
            time.sleep(0.05)

    assert captured["juicefs_volume_name"] == "andrew-3", captured
    assert "s3_prefix" not in captured, (
        "format_volume should NOT receive an s3_prefix kwarg; it must "
        "go through juicefs_volume_name instead.  Captured: " + str(captured)
    )

    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["s3_prefix"] == "andrew-3"
    assert body["juicefs_volume_name"] == "andrew-3"
    assert body["s3_bucket"] == "imbue-openhost"
    assert body["s3_region"] == "us-west-2"


@pytest.mark.asyncio
async def test_test_connection_requires_fields(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/test_connection",
        form={"s3_bucket": "b"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_rejects_invalid_s3_prefix(app):
    """The pre-flight endpoint validates s3_prefix shape with the same rules as the switch POST and rejects fail-fast — the head_bucket round-trip must NOT be made on an invalid prefix."""
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials") as mocked:
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "s3_prefix": "a/b",
            },
        )
        body = await resp.get_json()
        assert resp.status_code == 400, body
        assert "s3_prefix" in body["error"], body
        mocked.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_surfaces_errors(app):
    """A failed reachability check returns 400 with the underlying error string so the dashboard can surface it next to the form."""
    client = app.test_client()
    with mock.patch.object(
        archive_backend,
        "test_s3_credentials",
        return_value="bucket not found",
    ):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
            },
        )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["ok"] is False
    assert "bucket not found" in body["error"]


@pytest.mark.asyncio
async def test_list_archive_apps_heuristic_precision(app, cfg):
    """The heuristic deciding which apps to stop during a switch matches exactly ``app_archive = true`` or ``access_all_data = true``, not the substring "true" anywhere in the manifest, so a routine switch doesn't needlessly bounce every app with an unrelated boolean opt-in."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.executemany(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, '1.0', ?, ?, 'running', ?)",
            [
                ("real-archiver", "/r/a", 19501, "[data]\napp_archive = true\n"),
                (
                    "all-access",
                    "/r/aa",
                    19502,
                    "[data]\naccess_all_data = true\n",
                ),
                (
                    "innocent",
                    "/r/i",
                    19503,
                    "[data]\napp_archive = false\napp_data = true\n",
                ),
                ("plain", "/r/p", 19504, "[data]\napp_data = true\n"),
            ],
        )
        db.commit()
    finally:
        db.close()

    hook = routes._build_hook(app)
    matched = sorted(hook.list_app_archive_apps())
    assert matched == ["all-access", "real-archiver"], matched


@pytest.mark.asyncio
async def test_reload_app_refuses_when_archive_unhealthy(app, cfg):
    """An archive-using app cannot be reloaded while the configured archive backend is unhealthy; without this guard, provision_data would write to the underlying empty mount-point and lose those writes once the mount came back."""

    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('archived', '1.0', '/r/archived', 19601, 'running', "
            "'[data]\napp_archive = true\n')"
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_name>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    resp = await client.post("/reload_app/archived")
    assert resp.status_code == 503
    body = await resp.get_json()
    assert "Archive backend is not healthy" in body["error"]


@pytest.mark.asyncio
async def test_reload_app_allows_non_archive_when_archive_unhealthy(app, cfg):
    """An app that doesn't use the archive tier must still be reloadable when the archive backend is unhealthy — the precheck is targeted, not a blanket lock-out."""

    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('plain', '1.0', '/r/plain', 19602, 'running', "
            "'[data]\napp_data = true\n')"
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_name>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch("compute_space.web.routes.api.apps.stop_app_process"),
        mock.patch("compute_space.web.routes.api.apps.reload_app_background"),
    ):
        resp = await client.post("/reload_app/plain")
        assert resp.status_code != 503, await resp.get_data(as_text=True)


@pytest.mark.asyncio
async def test_add_app_refuses_archive_app_when_backend_disabled(app, cfg, tmp_path):
    """An app whose manifest opts into ``app_archive`` cannot be installed while the archive backend is at its v7-default 'disabled' state; the route returns 400 (operator-actionable, points at System tab) rather than 503 because this is a permanent rejection until the operator picks a backend."""

    db = sqlite3.connect(cfg.db_path)
    try:
        backend = db.execute("SELECT backend FROM archive_backend WHERE id=1").fetchone()[0]
    finally:
        db.close()
    assert backend == "disabled", "test setup: expected fresh seed"

    archive_manifest = AppManifest(
        name="probe",
        version="1.0",
        description="archive probe",
        runtime_type="serverfull",
        container_image="Dockerfile",
        container_port=8080,
        container_command=None,
        memory_mb=128,
        cpu_millicores=100,
        gpu=False,
        app_data=True,
        app_archive=True,
        access_all_data=False,
        access_vm_data=False,
        app_temp_data=False,
        public_paths=["/"],
        capabilities=[],
        devices=[],
        permissions_v2=[],
        port_mappings=[],
        provides_services=[],
        provides_services_v2=[],
        requires_services={},
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )
    fake_clone_dir = str(tmp_path / "clone")
    os.makedirs(fake_clone_dir)

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/api/add_app",
        view_func=apps_routes.api_add_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=archive_manifest),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
    ):
        resp = await client.post(
            "/api/add_app",
            form={
                "repo_url": "https://example.invalid/repo",
                "app_name": "probe",
                "clone_dir": fake_clone_dir,
                "grant_permissions": "",
            },
        )
    assert resp.status_code == 400, await resp.get_data(as_text=True)
    body = await resp.get_json()
    assert "archive backend" in body["error"].lower(), body
    assert "system page" in body["error"].lower(), body


@pytest.mark.asyncio
async def test_test_connection_succeeds(app):
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value=None):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
            },
        )
    assert resp.status_code == 200
    assert (await resp.get_json())["ok"] is True


def test_manifest_requires_archive_only_matches_app_archive_true():
    """``manifest_requires_archive`` (used by install/reload gates) keys on ``app_archive = true`` only — ``access_all_data = true`` alone does NOT qualify, otherwise apps like the backup app would be blocked on archive-less zones. Companion to ``manifest_uses_archive`` (which gates the backend-switch stop flow on either flag)."""
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\naccess_all_data = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_requires_archive("")
    assert not archive_backend.manifest_requires_archive("[data]\napp_archive = false\napp_data = true\n")
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\naccess_all_data = true\n")


@pytest.mark.asyncio
async def test_add_app_allows_access_all_data_when_backend_disabled(app, cfg, tmp_path):
    """An ``access_all_data = true`` app (with ``app_archive`` unset/false) must be installable while the archive backend is 'disabled' — ``access_all_data`` is permissive, so refusing the install would lock out apps like the backup app on every fresh archive-less zone."""

    db = sqlite3.connect(cfg.db_path)
    try:
        backend = db.execute("SELECT backend FROM archive_backend WHERE id=1").fetchone()[0]
    finally:
        db.close()
    assert backend == "disabled"

    aad_manifest = AppManifest(
        name="seer",
        version="1.0",
        description="access_all_data probe",
        runtime_type="serverfull",
        container_image="Dockerfile",
        container_port=8080,
        container_command=None,
        memory_mb=128,
        cpu_millicores=100,
        gpu=False,
        app_data=True,
        app_archive=False,
        access_all_data=True,
        access_vm_data=False,
        app_temp_data=False,
        public_paths=["/"],
        capabilities=[],
        devices=[],
        permissions_v2=[],
        port_mappings=[],
        provides_services=[],
        provides_services_v2=[],
        requires_services={},
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )
    fake_clone_dir = str(tmp_path / "clone-aad")
    os.makedirs(fake_clone_dir)

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/api/add_app",
        view_func=apps_routes.api_add_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=aad_manifest),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "insert_and_deploy", return_value="seer"),
    ):
        resp = await client.post(
            "/api/add_app",
            form={
                "repo_url": "https://example.invalid/repo",
                "app_name": "seer",
                "clone_dir": fake_clone_dir,
                "grant_permissions": "",
            },
        )
    body_text = await resp.get_data(as_text=True)
    assert resp.status_code != 400 or "archive" not in body_text.lower(), body_text
    assert resp.status_code != 503 or "archive" not in body_text.lower(), body_text


@pytest.mark.asyncio
async def test_reload_app_allows_access_all_data_when_archive_unhealthy(app, cfg):
    """An ``access_all_data`` app must be reloadable when the archive backend is unhealthy — the reload-time precheck is for apps that *require* the archive (``app_archive = true``); access_all_data apps just see the archive when it's available."""

    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('seer', '1.0', '/r/seer', 19603, 'running', "
            "'[data]\naccess_all_data = true\n')"
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_name>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch("compute_space.web.routes.api.apps.stop_app_process"),
        mock.patch("compute_space.web.routes.api.apps.reload_app_background"),
    ):
        resp = await client.post("/reload_app/seer")
        body = await resp.get_data(as_text=True)
        assert resp.status_code != 503 or "Archive backend is not healthy" not in body, body
