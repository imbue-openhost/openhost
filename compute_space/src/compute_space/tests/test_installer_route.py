"""Integration tests for the installer dispatch inside the v2 service proxy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

from compute_space.config import set_active_config
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import InstallError
from compute_space.core.installer import InstallResult
from compute_space.db.connection import init_db
from compute_space.web.auth.middleware import provide_app_id
from compute_space.web.auth.middleware import provide_user
from compute_space.web.routes.services_v2 import services_v2_routes

from .conftest import _make_test_config

CALLER_APP = "openhost-catalog"
CALLER_TOKEN = "test-installer-caller-token"
CALLER_APP_ID = "TestCaller01"

CALLER_MANIFEST = """
[app]
name = "openhost-catalog"
version = "0.2.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/installer"
shortname = "installer"
version = ">=0.1.0"
grants = [{capability = "install", repo_url_prefix = "*"}]
"""


def _make_app(cfg) -> Litestar:
    set_active_config(cfg)
    return Litestar(
        route_handlers=services_v2_routes,
        dependencies={"user": Provide(provide_user), "caller_app_id": Provide(provide_app_id)},
        openapi_config=None,
    )


def _seed_caller(
    db_path: str,
    app_name: str = CALLER_APP,
    app_id: str = CALLER_APP_ID,
    token: str = CALLER_TOKEN,
    manifest_raw: str = CALLER_MANIFEST,
) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, installed_by, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (app_id, app_name, "0.0.0", f"/tmp/{app_name}", 19500, "running", manifest_raw),
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, token_hash),
        )
        db.commit()
    finally:
        db.close()


def _grant_installer(db_path: str, app_id: str, repo_url_prefix: str = "*") -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with mock.patch("compute_space.core.auth.permissions_v2.get_db", return_value=conn):
            grant_permission_v2(
                app_id,
                INSTALLER_SERVICE_URL,
                {"capability": "install", "repo_url_prefix": repo_url_prefix},
            )
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path: Path):
    cfg_obj = _make_test_config(tmp_path, port=20500)
    set_active_config(cfg_obj)
    return cfg_obj


@pytest.fixture
def app(cfg):
    init_db(cfg.db_path)
    _seed_caller(cfg.db_path)
    yield _make_app(cfg)


def _url(endpoint: str) -> str:
    return "/api/services/v2/call/installer/" + endpoint.lstrip("/")


def _headers(token: str = CALLER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- /install --------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_without_grant_returns_permission_required(app, cfg):
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            _url("install"),
            headers=_headers(),
            content=json.dumps({"repo_url": "https://github.com/imbue-openhost/openhost-catalog"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "permission_required"
    assert body["required_grant"]["grant"]["capability"] == "install"
    assert body["required_grant"]["grant"]["repo_url_prefix"] == "*"


@pytest.mark.asyncio
async def test_install_with_non_matching_grant_denied(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="https://github.com/imbue-openhost/")
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            _url("install"),
            headers=_headers(),
            content=json.dumps({"repo_url": "https://github.com/evil/badapp"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "permission_required"


NARROW_CALLER_APP = "narrow-catalog"
NARROW_CALLER_TOKEN = "narrow-test-token"
NARROW_CALLER_MANIFEST = """
[app]
name = "narrow-catalog"
version = "0.2.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/installer"
shortname = "installer"
version = ">=0.1.0"
grants = [{capability = "install", repo_url_prefix = "https://github.com/imbue-openhost/"}]
"""


@pytest.mark.asyncio
async def test_proposed_grant_uses_manifest_declared_prefix(cfg):
    init_db(cfg.db_path)
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )

    app = _make_app(cfg)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            _url("install"),
            headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
            content=json.dumps({"repo_url": "https://github.com/imbue-openhost/openhost-gemini"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["required_grant"]["grant"] == {
        "capability": "install",
        "repo_url_prefix": "https://github.com/imbue-openhost/",
    }


@pytest.mark.asyncio
async def test_proposed_grant_falls_back_when_manifest_prefix_doesnt_match(cfg):
    init_db(cfg.db_path)
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )

    app = _make_app(cfg)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            _url("install"),
            headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
            content=json.dumps({"repo_url": "https://gitlab.com/something/else"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["required_grant"]["grant"]["repo_url_prefix"] == "https://gitlab.com/something/else"


@pytest.mark.asyncio
async def test_install_with_matching_grant_succeeds(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")

    async def fake_install(repo_url, config, db, *, app_name=None, installed_by=None):
        return InstallResult(app_name="newapp", status="building")

    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                _url("install"),
                headers=_headers(),
                content=json.dumps({"repo_url": "https://github.com/anyone/anything"}),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "app_name": "newapp", "status": "building"}


@pytest.mark.asyncio
async def test_install_propagates_install_error_status_code(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")

    async def fake_install(*args, **kwargs):
        raise InstallError("manifest invalid", status_code=400)

    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                _url("install"),
                headers=_headers(),
                content=json.dumps({"repo_url": "https://github.com/foo/bar"}),
            )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "install_failed"
    assert "manifest invalid" in body["message"]


@pytest.mark.asyncio
async def test_install_rejects_missing_repo_url(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            _url("install"),
            headers=_headers(),
            content=json.dumps({"app_name": "no-url-supplied"}),
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_install_rejects_non_json_body(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(_url("install"), headers=_headers(), content="not-json")
    assert resp.status_code == 400


# --- /status/<name> --------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_404_for_unknown_app(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(_url("status/nope"), headers=_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_403_for_app_not_installed_by_caller(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (new_app_id(), "dashboard-installed-app", "1.0.0", "/tmp/dash", 19501, "running"),
        )
        db.commit()
    finally:
        db.close()
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(_url("status/dashboard-installed-app"), headers=_headers())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_returns_app_state_for_caller_installs(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_app_id(), "catalog-installed-app", "1.0.0", "/tmp/c", 19502, "running", CALLER_APP_ID, None),
        )
        db.commit()
    finally:
        db.close()
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(_url("status/catalog-installed-app"), headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "running", "error": None}


# --- Shortname not declared ------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_shortname_returns_404(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/services/v2/call/not-installed/anything",
            headers=_headers(),
            content=json.dumps({}),
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "shortname_not_declared"


# --- Unknown installer sub-endpoint ----------------------------------------


@pytest.mark.asyncio
async def test_unknown_installer_subpath_returns_404(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(_url("wat"), headers=_headers(), content=json.dumps({}))
    assert resp.status_code == 404
