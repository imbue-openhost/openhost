"""Tests for the ``/api/storage/archive_backend`` endpoints.

Drives the routes through Quart's test client so the full
form-parsing + JSON serialisation paths are exercised.  The actual
JuiceFS subprocess work in ``switch_backend`` is mocked at the
core-module boundary so these tests stay fast and don't need a
real S3 bucket.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.archive_backend as routes
from compute_space.core import archive_backend
from compute_space.db.connection import init_db

from .conftest import _FakeApp, _make_test_config


def _make_app(cfg) -> Quart:  # noqa: ANN001
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    # Wire the unwrapped endpoints so login_required doesn't bounce.
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


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_seeded_local_state(app):
    """A fresh DB returns the seeded ``local`` row with a resolved
    archive_dir but no S3 fields set."""
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["backend"] == "local"
    assert body["state"] == "idle"
    assert body["s3_bucket"] is None
    assert body["archive_dir"].endswith("/persistent_data/app_archive")
    # The secret access key field must NEVER be in the response.
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


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_local_to_s3_returns_202_and_runs_switch(app, cfg):
    """Switching local -> s3 returns 202 with state=switching; the
    background thread eventually flips the DB row to s3/idle.
    """
    # Pre-create the JuiceFS mount target so the (mocked) mount
    # leaves us with somewhere to copy into.
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

        # Wait for the worker thread to finish.  The switch is small
        # (no real S3 work) so this should settle very quickly.
        deadline = time.time() + 5
        while time.time() < deadline:
            db = sqlite3.connect(cfg.db_path)
            try:
                row = db.execute(
                    "SELECT backend, state FROM archive_backend WHERE id=1"
                ).fetchone()
            finally:
                db.close()
            if row[0] == "s3" and row[1] == "idle":
                break
            time.sleep(0.05)

    # GET reflects the new state and still redacts the secret.
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["state"] == "idle"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body
    # And the resolved archive_dir now points at the JuiceFS mount.
    assert body["archive_dir"] == juicefs_mount


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_requires_fields(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/test_connection",
        form={"s3_bucket": "b"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_surfaces_errors(app):
    """A failed reachability check returns 400 with the underlying
    error string so the dashboard can surface it next to the form.
    """
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
