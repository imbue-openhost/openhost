"""Integration tests for the installer dispatch inside the v2 service proxy.

Tests the request-routing surface: shortname dispatch, version specifier
handling, permission check, endpoint dispatch (install / status / logs),
and the ``{installed_by != caller}`` visibility scoping.  The actual
install side-effect (clone + build + run) is patched out — that's
exercised in the end-to-end harness, not here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import InstallError
from compute_space.core.installer import InstallResult
from compute_space.core.permissions_v2 import grant_permission_v2
from compute_space.db.connection import init_db
from compute_space.web.routes.services_v2 import services_v2_bp

from .conftest import _FakeApp
from .conftest import _make_test_config

CALLER_APP = "openhost-catalog"
CALLER_TOKEN = "test-installer-caller-token"

# Manifest the caller declares.  Crucially includes a
# [[services.v2.consumes]] entry pointing at the installer service so
# /api/services/v2/call/installer/... resolves.
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


def _make_app(cfg) -> Quart:  # noqa: ANN001
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    app.register_blueprint(services_v2_bp)
    return app


def _seed_caller(
    db_path: str,
    app_name: str = CALLER_APP,
    token: str = CALLER_TOKEN,
    manifest_raw: str = CALLER_MANIFEST,
) -> None:
    """Insert the caller's apps row (with manifest_raw) + app_tokens row
    so app_auth_required can resolve the bearer to the caller name AND
    lookup_shortname can find the installer shortname declaration."""
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        db.execute(
            """INSERT INTO apps
                 (name, version, repo_path, local_port, status, installed_by, manifest_raw)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (app_name, "0.0.0", f"/tmp/{app_name}", 19500, "running", manifest_raw),
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.execute(
            "INSERT INTO app_tokens (app_name, token_hash) VALUES (?, ?)",
            (app_name, token_hash),
        )
        db.commit()
    finally:
        db.close()


def _grant_installer(db_path: str, app_name: str, repo_url_prefix: str = "*") -> None:
    """Add a permissions_v2 row letting ``app_name`` use the installer service."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with mock.patch("compute_space.core.permissions_v2.get_db", return_value=conn):
            grant_permission_v2(
                app_name,
                INSTALLER_SERVICE_URL,
                {"capability": "install", "repo_url_prefix": repo_url_prefix},
            )
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20500)


@pytest.fixture
def app(cfg):
    init_db(_FakeApp(cfg.db_path))
    _seed_caller(cfg.db_path)
    yield _make_app(cfg)


def _url(endpoint: str) -> str:
    """Build the v2 shortname-call URL for the installer."""
    return "/api/services/v2/call/installer/" + endpoint.lstrip("/")


def _headers(token: str = CALLER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- /install --------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_without_grant_returns_permission_required(app, cfg):
    client = app.test_client()
    resp = await client.post(
        _url("install"),
        headers=_headers(),
        data=json.dumps({"repo_url": "https://github.com/imbue-openhost/openhost-catalog"}),
    )
    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["error"] == "permission_required"
    assert body["required_grant"]["grant"]["capability"] == "install"
    # The proposed grant must come from the consumer's manifest-declared
    # prefix ("*" in CALLER_MANIFEST), not the verbatim requested URL —
    # otherwise the owner gets a fresh approval prompt per repo.
    assert body["required_grant"]["grant"]["repo_url_prefix"] == "*"


@pytest.mark.asyncio
async def test_install_with_non_matching_grant_denied(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="https://github.com/imbue-openhost/")
    client = app.test_client()
    resp = await client.post(
        _url("install"),
        headers=_headers(),
        data=json.dumps({"repo_url": "https://github.com/evil/badapp"}),
    )
    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["error"] == "permission_required"


# Caller whose manifest declares the GitHub-org prefix the catalog actually ships
# with.  Used by test_proposed_grant_uses_manifest_declared_prefix to exercise
# the org-scoped (rather than wildcard) path.
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
    """When the consumer's manifest declares a broad prefix, a denial for an
    URL that matches that prefix must propose the broad grant — not a fresh
    per-URL grant — so a single approval covers every install."""
    init_db(_FakeApp(cfg.db_path))
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )

    app = _make_app(cfg)
    client = app.test_client()
    resp = await client.post(
        _url("install"),
        headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"repo_url": "https://github.com/imbue-openhost/openhost-gemini"}),
    )
    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["required_grant"]["grant"] == {
        "capability": "install",
        "repo_url_prefix": "https://github.com/imbue-openhost/",
    }


@pytest.mark.asyncio
async def test_proposed_grant_falls_back_when_manifest_prefix_doesnt_match(cfg):
    """If the consumer's manifest only declares a narrow prefix that does NOT
    cover the requested URL, fall back to a per-URL grant rather than
    suggesting a grant that wouldn't help."""
    init_db(_FakeApp(cfg.db_path))
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )

    app = _make_app(cfg)
    client = app.test_client()
    resp = await client.post(
        _url("install"),
        headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"repo_url": "https://gitlab.com/something/else"}),
    )
    assert resp.status_code == 403
    body = await resp.get_json()
    assert body["required_grant"]["grant"]["repo_url_prefix"] == "https://gitlab.com/something/else"


@pytest.mark.asyncio
async def test_install_with_matching_grant_succeeds(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="*")

    async def fake_install(repo_url, config, db, *, app_name=None, installed_by=None):
        return InstallResult(app_name="newapp", status="building")

    client = app.test_client()
    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        resp = await client.post(
            _url("install"),
            headers=_headers(),
            data=json.dumps({"repo_url": "https://github.com/anyone/anything"}),
        )
    assert resp.status_code == 200, await resp.get_data(as_text=True)
    body = await resp.get_json()
    assert body == {"ok": True, "app_name": "newapp", "status": "building"}


@pytest.mark.asyncio
async def test_install_propagates_install_error_status_code(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="*")

    async def fake_install(*args, **kwargs):
        raise InstallError("manifest invalid", status_code=400)

    client = app.test_client()
    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        resp = await client.post(
            _url("install"),
            headers=_headers(),
            data=json.dumps({"repo_url": "https://github.com/foo/bar"}),
        )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["error"] == "install_failed"
    assert "manifest invalid" in body["message"]


@pytest.mark.asyncio
async def test_install_rejects_missing_repo_url(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="*")
    client = app.test_client()
    resp = await client.post(
        _url("install"),
        headers=_headers(),
        data=json.dumps({"app_name": "no-url-supplied"}),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_install_rejects_non_json_body(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="*")
    client = app.test_client()
    resp = await client.post(_url("install"), headers=_headers(), data="not-json")
    assert resp.status_code == 400


# --- /status/<name> --------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_404_for_unknown_app(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP)
    client = app.test_client()
    resp = await client.get(_url("status/nope"), headers=_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_403_for_app_not_installed_by_caller(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, NULL)""",
            ("dashboard-installed-app", "1.0.0", "/tmp/dash", 19501, "running"),
        )
        db.commit()
    finally:
        db.close()
    client = app.test_client()
    resp = await client.get(_url("status/dashboard-installed-app"), headers=_headers())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_returns_app_state_for_caller_installs(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (name, version, repo_path, local_port, status, installed_by, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("catalog-installed-app", "1.0.0", "/tmp/c", 19502, "running", CALLER_APP, None),
        )
        db.commit()
    finally:
        db.close()
    client = app.test_client()
    resp = await client.get(_url("status/catalog-installed-app"), headers=_headers())
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body == {"status": "running", "error": None}


# --- Shortname not declared ------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_shortname_returns_404(app, cfg):
    """Calls to /api/services/v2/call/<other>/... 404 because the caller's
    manifest only declares the installer shortname."""
    _grant_installer(cfg.db_path, CALLER_APP, repo_url_prefix="*")
    client = app.test_client()
    resp = await client.post(
        "/api/services/v2/call/not-installed/anything",
        headers=_headers(),
        data=json.dumps({}),
    )
    assert resp.status_code == 404
    body = await resp.get_json()
    assert body["error"] == "shortname_not_declared"


# --- Unknown installer sub-endpoint ----------------------------------------


@pytest.mark.asyncio
async def test_unknown_installer_subpath_returns_404(app, cfg):
    _grant_installer(cfg.db_path, CALLER_APP)
    client = app.test_client()
    resp = await client.post(_url("wat"), headers=_headers(), data=json.dumps({}))
    assert resp.status_code == 404
