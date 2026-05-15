"""Tests for the ``/api/storage/archive_backend`` endpoints."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.apps as apps_routes
import compute_space.web.routes.api.archive_backend as routes
from compute_space.core import archive_backend
from compute_space.core.app_id import new_app_id
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
        "/api/storage/archive_backend/configure",
        view_func=routes.configure_archive_backend.__wrapped__,  # type: ignore[attr-defined]
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


# --- GET state ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_seeded_disabled_state(app):
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["backend"] == "disabled"
    assert body["s3_bucket"] is None
    assert body["archive_dir"] is None
    assert body["meta_dumps"] is None
    assert "s3_secret_access_key" not in body


@pytest.mark.asyncio
async def test_get_redacts_secret_when_s3(app):
    """In s3 mode the access_key_id is visible (so the dashboard can show
    the AKIA prefix) but the secret is never returned."""
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


@pytest.mark.asyncio
async def test_get_surfaces_meta_db_path(app):
    """meta_db_path always surfaces (under juicefs/state/) so the operator
    can pre-plan their backup story."""
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["meta_db_path"].endswith("/juicefs/state/meta.db")


@pytest.mark.asyncio
async def test_get_surfaces_meta_dumps_when_s3(app):
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_prefix='zone-a', s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
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
    assert body["meta_dumps"]["count"] == 42
    assert body["meta_dumps"]["latest_at"] == "2026-05-01T18:00:00Z"


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_disabled(app):
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["meta_dumps"] is None


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_s3_list_failure(app):
    """``meta_dumps: null`` distinguishes "status unavailable" from "no dumps yet"."""
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
    assert body["meta_dumps"] is None


# --- configure route ------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_requires_creds(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/configure",
        form={"s3_bucket": "b"},
    )
    assert resp.status_code == 400
    assert "Missing required fields" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_configure_rejects_invalid_s3_prefix(app):
    """Malformed prefix (path traversal, whitespace, NUL, multi-segment,
    uppercase, underscore, too short, leading/trailing dash, dot) is
    rejected at the route layer because it's used directly as the
    JuiceFS volume name (regex ``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``)."""
    client = app.test_client()
    for bad in (
        "../etc",
        "with space",
        "embedded\x00null",
        "a/b",
        "UPPER",
        "under_score",
        "ab",
        "-leading",
        "trailing-",
        "with.dot",
    ):
        resp = await client.post(
            "/api/storage/archive_backend/configure",
            form={
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
async def test_configure_rejects_when_already_configured(app):
    """Configure is one-shot: once the backend is 's3', subsequent
    configure calls return 409.  Reconfiguration is intentionally not
    supported."""
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b'")
        db.commit()
    finally:
        db.close()
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/configure",
        form={"s3_bucket": "b2", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_configure_happy_path(app):
    """Format + mount + DB UPDATE; response carries the persisted state."""
    client = app.test_client()
    with mock.patch.object(archive_backend, "configure_backend") as mock_configure:
        # Side-effect: actually update the DB so read_state returns s3.
        def side_effect(config, db, **kwargs):
            db.execute(
                "UPDATE archive_backend SET backend='s3', s3_bucket=?, "
                "s3_access_key_id=?, s3_secret_access_key=?, s3_prefix=? WHERE id=1",
                (
                    kwargs["s3_bucket"],
                    kwargs["s3_access_key_id"],
                    kwargs["s3_secret_access_key"],
                    kwargs.get("s3_prefix"),
                ),
            )
            db.commit()

        mock_configure.side_effect = side_effect

        resp = await client.post(
            "/api/storage/archive_backend/configure",
            form={
                "s3_bucket": "mybucket",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "secret",
                "s3_prefix": "andrew-3",
            },
        )
    assert resp.status_code == 200, await resp.get_data(as_text=True)
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body


# --- test_connection ------------------------------------------------------


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
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/test_connection",
        form={
            "s3_bucket": "b",
            "s3_access_key_id": "a",
            "s3_secret_access_key": "s",
            "s3_prefix": "UPPER",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_surfaces_errors(app):
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value="bucket not found"):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
        )
    assert resp.status_code == 400
    assert "bucket not found" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_test_connection_succeeds(app):
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value=None):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
        )
    assert resp.status_code == 200
    assert (await resp.get_json())["ok"] is True


# --- manifest predicates --------------------------------------------------


def test_manifest_requires_archive_only_matches_app_archive_true():
    """``manifest_requires_archive`` (install/reload gates) keys on
    ``app_archive = true`` only; ``access_all_data = true`` doesn't qualify
    so apps like the backup app aren't blocked on archive-less zones."""
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\naccess_all_data = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_requires_archive("")
    # Anchor on TOML key=value shape so substring matching can't false-match.
    assert not archive_backend.manifest_requires_archive("[data]\napp_archive = false\napp_data = true\n")
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\naccess_all_data = true\n")


def test_manifest_uses_archive_matches_either_flag():
    """``manifest_uses_archive`` is broader: either flag qualifies, since
    both result in the archive mount being granted to the container."""
    assert archive_backend.manifest_uses_archive("[data]\napp_archive = true\n")
    assert archive_backend.manifest_uses_archive("[data]\naccess_all_data = true\n")
    assert not archive_backend.manifest_uses_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_uses_archive("[data]\napp_archive = false\napp_data = true\n")


# --- install/reload gates -------------------------------------------------


def _archive_manifest(name: str, *, app_archive: bool, access_all_data: bool = False) -> AppManifest:
    return AppManifest(
        name=name,
        version="1.0",
        description="probe",
        runtime_type="serverfull",
        container_image="Dockerfile",
        container_port=8080,
        container_command=None,
        memory_mb=128,
        cpu_millicores=100,
        gpu=False,
        app_data=True,
        app_archive=app_archive,
        access_all_data=access_all_data,
        access_vm_data=False,
        app_temp_data=False,
        public_paths=["/"],
        capabilities=[],
        devices=[],
        consumes_services_v2=[],
        port_mappings=[],
        provides_services_v2=[],
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )


def _add_app_test_app(cfg) -> Quart:
    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/api/add_app",
        view_func=apps_routes.api_add_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    return test_app


@pytest.mark.asyncio
async def test_add_app_refuses_archive_app_when_backend_disabled(app, cfg, tmp_path):
    """An app with ``app_archive = true`` cannot be installed while the
    backend is 'disabled'.  400 (operator-actionable: configure S3) rather
    than 503 (transient retry)."""
    fake_clone_dir = str(tmp_path / "clone")
    os.makedirs(fake_clone_dir)
    test_app = _add_app_test_app(cfg)
    client = test_app.test_client()
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=_archive_manifest("probe", app_archive=True)),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
    ):
        resp = await client.post(
            "/api/add_app",
            form={
                "repo_url": "https://example.invalid/repo",
                "app_name": "probe",
                "clone_dir": fake_clone_dir,
            },
        )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert "S3" in body["error"] or "system page" in body["error"].lower()


@pytest.mark.asyncio
async def test_add_app_allows_access_all_data_when_backend_disabled(app, cfg, tmp_path):
    """``access_all_data = true`` (without ``app_archive``) does NOT need a
    configured archive backend — the app silently goes without the mount."""
    fake_clone_dir = str(tmp_path / "clone-aad")
    os.makedirs(fake_clone_dir)
    test_app = _add_app_test_app(cfg)
    client = test_app.test_client()
    with (
        mock.patch.object(
            apps_routes,
            "parse_manifest",
            return_value=_archive_manifest("seer", app_archive=False, access_all_data=True),
        ),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "insert_and_deploy", return_value="seer"),
    ):
        resp = await client.post(
            "/api/add_app",
            form={
                "repo_url": "https://example.invalid/repo",
                "app_name": "seer",
                "clone_dir": fake_clone_dir,
            },
        )
    body_text = await resp.get_data(as_text=True)
    # Must NOT reject with archive-related 400/503.
    assert resp.status_code != 400 or "archive" not in body_text.lower(), body_text
    assert resp.status_code != 503 or "archive" not in body_text.lower(), body_text


@pytest.mark.asyncio
async def test_reload_app_refuses_when_archive_unhealthy(app, cfg):
    """An archive-using app cannot be reloaded while the JuiceFS mount is
    unhealthy; without this guard, provision_data would write to the
    underlying empty mount-point and lose those writes once the mount
    came back."""
    archived_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, 'archived', '1.0', '/r/archived', 19601, 'running', "
            "'[data]\napp_archive = true\n')",
            (archived_id,),
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_id>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    resp = await client.post(f"/reload_app/{archived_id}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_reload_app_allows_access_all_data_when_archive_unhealthy(app, cfg):
    """access_all_data apps can still reload while the archive is unhealthy
    — they just see the archive when it's available."""
    seer_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, 'seer', '1.0', '/r/seer', 19603, 'running', "
            "'[data]\naccess_all_data = true\n')",
            (seer_id,),
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_id>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch("compute_space.web.routes.api.apps.stop_app_process"),
        mock.patch("compute_space.web.routes.api.apps.reload_app_background"),
    ):
        resp = await client.post(f"/reload_app/{seer_id}")
        body = await resp.get_data(as_text=True)
        assert resp.status_code != 503 or "archive" not in body.lower()
