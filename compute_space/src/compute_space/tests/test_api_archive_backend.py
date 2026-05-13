"""Tests for the ``/api/storage/archive_backend`` endpoints."""

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
import compute_space.web.routes.api.archive_backend as routes
from compute_space.config import set_active_config
from compute_space.core import archive_backend
from compute_space.core.app_id import new_app_id
from compute_space.core.manifest import AppManifest
from compute_space.db.connection import init_db

from .conftest import _make_test_config


async def _user_stub() -> dict[str, str]:
    return {"sub": "owner", "username": "owner"}


def _make_app(cfg, route_handlers) -> Litestar:  # noqa: ANN001
    set_active_config(cfg)
    return Litestar(
        route_handlers=route_handlers,
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )


@pytest.fixture
def cfg(tmp_path: Path):
    cfg_obj = _make_test_config(tmp_path, port=20400)
    set_active_config(cfg_obj)
    return cfg_obj


@pytest.fixture
def app(cfg):
    init_db(cfg.db_path)
    yield _make_app(cfg, [routes.get_archive_backend, routes.test_connection, routes.configure_archive_backend])


@pytest.fixture
def db_path(cfg) -> str:
    return cfg.db_path


# --- GET state ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_seeded_disabled_state(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/storage/archive_backend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "disabled"
    assert body["s3_bucket"] is None
    assert body["archive_dir"] is None
    assert body["meta_dumps"] is None
    assert "s3_secret_access_key" not in body


@pytest.mark.asyncio
async def test_get_redacts_secret_when_s3(app, db_path):
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIASOMETHING', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/storage/archive_backend")
    body = resp.json()
    assert body["s3_access_key_id"] == "AKIASOMETHING"
    assert "s3_secret_access_key" not in body


@pytest.mark.asyncio
async def test_get_surfaces_meta_db_path(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/storage/archive_backend")
    body = resp.json()
    assert body["meta_db_path"].endswith("/juicefs/state/meta.db")


@pytest.mark.asyncio
async def test_get_surfaces_meta_dumps_when_s3(app, db_path):
    db = sqlite3.connect(db_path)
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
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=summary):
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/api/storage/archive_backend")
    body = resp.json()
    assert body["meta_dumps"]["count"] == 42
    assert body["meta_dumps"]["latest_at"] == "2026-05-01T18:00:00Z"


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_disabled(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/storage/archive_backend")
    body = resp.json()
    assert body["meta_dumps"] is None


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_s3_list_failure(app, db_path):
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=None):
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/api/storage/archive_backend")
    body = resp.json()
    assert body["meta_dumps"] is None


# --- configure route ------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_requires_creds(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/storage/archive_backend/configure",
            data={"s3_bucket": "b"},
        )
    assert resp.status_code == 400
    assert "Missing required fields" in resp.json()["error"]


@pytest.mark.asyncio
async def test_configure_rejects_invalid_s3_prefix(app):
    async with AsyncTestClient(app=app) as client:
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
                data={
                    "s3_bucket": "b",
                    "s3_access_key_id": "a",
                    "s3_secret_access_key": "s",
                    "s3_prefix": bad,
                },
            )
            body = resp.json()
            assert resp.status_code == 400, (bad, body)
            assert "s3_prefix" in body["error"], (bad, body)


@pytest.mark.asyncio
async def test_configure_rejects_when_already_configured(app, db_path):
    db = sqlite3.connect(db_path)
    try:
        db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b'")
        db.commit()
    finally:
        db.close()
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/storage/archive_backend/configure",
            data={"s3_bucket": "b2", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_configure_happy_path(app):
    with mock.patch.object(archive_backend, "configure_backend") as mock_configure:

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

        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/storage/archive_backend/configure",
                data={
                    "s3_bucket": "mybucket",
                    "s3_access_key_id": "AKIA",
                    "s3_secret_access_key": "secret",
                    "s3_prefix": "andrew-3",
                },
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "s3"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body


# --- test_connection ------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_requires_fields(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            data={"s3_bucket": "b"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_rejects_invalid_s3_prefix(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            data={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "s3_prefix": "UPPER",
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_surfaces_errors(app):
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value="bucket not found"):
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/storage/archive_backend/test_connection",
                data={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
            )
    assert resp.status_code == 400
    assert "bucket not found" in resp.json()["error"]


@pytest.mark.asyncio
async def test_test_connection_succeeds(app):
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value=None):
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/storage/archive_backend/test_connection",
                data={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
            )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --- manifest predicates --------------------------------------------------


def test_manifest_requires_archive_only_matches_app_archive_true():
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\naccess_all_data = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_requires_archive("")
    assert not archive_backend.manifest_requires_archive("[data]\napp_archive = false\napp_data = true\n")
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\naccess_all_data = true\n")


def test_manifest_uses_archive_matches_either_flag():
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
        provides_services=[],
        provides_services_v2=[],
        requires_services={},
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )


def _add_app_test_app(cfg) -> Litestar:
    set_active_config(cfg)
    return Litestar(
        route_handlers=[apps_routes.api_add_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )


@pytest.mark.asyncio
async def test_add_app_refuses_archive_app_when_backend_disabled(app, cfg, tmp_path):
    fake_clone_dir = str(tmp_path / "clone")
    os.makedirs(fake_clone_dir)
    test_app = _add_app_test_app(cfg)
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=_archive_manifest("probe", app_archive=True)),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
    ):
        async with AsyncTestClient(app=test_app) as client:
            resp = await client.post(
                "/api/add_app",
                data={
                    "repo_url": "https://example.invalid/repo",
                    "app_name": "probe",
                    "clone_dir": fake_clone_dir,
                    "grant_permissions": "",
                },
            )
    assert resp.status_code == 400
    body = resp.json()
    assert "S3" in body["error"] or "system page" in body["error"].lower()


@pytest.mark.asyncio
async def test_add_app_allows_access_all_data_when_backend_disabled(app, cfg, tmp_path):
    fake_clone_dir = str(tmp_path / "clone-aad")
    os.makedirs(fake_clone_dir)
    test_app = _add_app_test_app(cfg)
    with (
        mock.patch.object(
            apps_routes,
            "parse_manifest",
            return_value=_archive_manifest("seer", app_archive=False, access_all_data=True),
        ),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "insert_and_deploy", return_value="seer"),
    ):
        async with AsyncTestClient(app=test_app) as client:
            resp = await client.post(
                "/api/add_app",
                data={
                    "repo_url": "https://example.invalid/repo",
                    "app_name": "seer",
                    "clone_dir": fake_clone_dir,
                    "grant_permissions": "",
                },
            )
    body_text = resp.text
    assert resp.status_code != 400 or "archive" not in body_text.lower(), body_text
    assert resp.status_code != 503 or "archive" not in body_text.lower(), body_text


@pytest.mark.asyncio
async def test_reload_app_refuses_when_archive_unhealthy(app, cfg):
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

    set_active_config(cfg)
    test_app = Litestar(
        route_handlers=[apps_routes.reload_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    async with AsyncTestClient(app=test_app) as client:
        resp = await client.post(f"/reload_app/{archived_id}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_reload_app_allows_access_all_data_when_archive_unhealthy(app, cfg):
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

    set_active_config(cfg)
    test_app = Litestar(
        route_handlers=[apps_routes.reload_app],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    with (
        mock.patch("compute_space.web.routes.api.apps.stop_app_process"),
        mock.patch("compute_space.web.routes.api.apps.reload_app_background"),
    ):
        async with AsyncTestClient(app=test_app) as client:
            resp = await client.post(f"/reload_app/{seer_id}")
        body = resp.text
        assert resp.status_code != 503 or "archive" not in body.lower()
