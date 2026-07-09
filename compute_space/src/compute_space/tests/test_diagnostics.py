"""Tests for the diagnostics collectors and their HTTP endpoints."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core import diagnostics
from compute_space.core.app_id import new_app_id
from compute_space.core.diagnostics import DIAGNOSTICS_SCHEMA_VERSION
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.system import system_routes

_MINIMAL_MANIFEST = """
[app]
name = "myapp"
version = "2.3.4"

[runtime.container]
image = "docker.io/library/nginx:latest"
port = 80
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_app(
    db_path: str,
    name: str,
    *,
    status: str = "running",
    version: str = "1.0",
    manifest_raw: str | None = None,
    repo_path: str = "/r",
) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, ?, ?, 'serverfull', ?, 19500, ?, ?)",
            (app_id, name, version, repo_path, status, manifest_raw),
        )
        db.commit()
    finally:
        db.close()
    return app_id


# ─── unit tests for low-level collectors ─────────────────────────────────────


def test_collect_system_info_populated() -> None:
    info = diagnostics._collect_system_info()
    assert info.system  # e.g. "Linux"
    assert info.python_version
    assert info.python_implementation


def test_collect_dependencies_marks_missing() -> None:
    deps = diagnostics._collect_dependencies()
    # Every curated dependency must appear (installed or explicitly not).
    assert set(deps) == set(diagnostics._KEY_DEPENDENCIES)
    # litestar is a hard runtime dep, so it must resolve to a real version.
    assert deps["litestar"] != "(not installed)"


def test_manifest_fields_parses_version() -> None:
    version, runtime_type = diagnostics._manifest_fields(_MINIMAL_MANIFEST)
    assert version == "2.3.4"
    assert runtime_type == "serverfull"


def test_manifest_fields_handles_bad_manifest() -> None:
    assert diagnostics._manifest_fields("not valid toml [[[") == (None, None)
    assert diagnostics._manifest_fields(None) == (None, None)
    assert diagnostics._manifest_fields("") == (None, None)


def test_collect_container_runtime_no_podman() -> None:
    with patch("compute_space.core.diagnostics.shutil.which", return_value=None):
        info = diagnostics._collect_container_runtime()
    assert info.available is False
    assert info.rootless is None
    assert info.error is not None


def test_collect_container_runtime_rootless_true() -> None:
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"version": {"Version": "4.9.3"}, "host": {"security": {"rootless": True}}}),
        stderr="",
    )
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.version == "4.9.3"
    assert info.rootless is True
    assert info.error is None


def test_collect_container_runtime_non_json() -> None:
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.rootless is None
    assert info.error is not None


# ─── platform diagnostics endpoint ───────────────────────────────────────────


@pytest.fixture
def system_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(system_routes)) as c:
        yield c


def test_platform_diagnostics_requires_auth(system_client: TestClient[Litestar]) -> None:
    resp = system_client.get("/api/diagnostics")
    assert resp.status_code in (401, 403)


def test_platform_diagnostics_returns_bundle(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    resp = system_client.get("/api/diagnostics", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert body["generated_at"]
    assert body["zone_domain"] == cfg.zone_domain
    assert "openhost" in body
    assert "system" in body
    assert body["system"]["python_version"]
    assert "container_runtime" in body
    assert "dependencies" in body
    assert body["dependencies"]["litestar"]
    # The seeded app appears, with the version re-parsed from its manifest.
    names = {a["name"]: a for a in body["apps"]}
    assert "myapp" in names
    assert names["myapp"]["version"] == "2.3.4"


def test_platform_diagnostics_download_header(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    resp = system_client.get("/api/diagnostics?download=1", cookies=cookies)
    assert resp.status_code == 200
    disp = resp.headers.get("content-disposition", "")
    assert "attachment" in disp
    assert ".json" in disp


def test_platform_diagnostics_version_falls_back_to_column(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    # No manifest_raw -> version must fall back to the stored apps.version column.
    _seed_app(cfg.db_path, "noman", version="9.9", manifest_raw=None)
    resp = system_client.get("/api/diagnostics", cookies=cookies)
    names = {a["name"]: a for a in resp.json()["apps"]}
    assert names["noman"]["version"] == "9.9"


# ─── per-app diagnostics endpoint ─────────────────────────────────────────────


@pytest.fixture
def apps_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


def test_app_diagnostics_requires_auth(apps_client: TestClient[Litestar]) -> None:
    resp = apps_client.get(f"/api/app_diagnostics/{new_app_id()}")
    assert resp.status_code in (401, 403)


def test_app_diagnostics_404_when_missing(apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = apps_client.get(f"/api/app_diagnostics/{new_app_id()}", cookies=cookies)
    assert resp.status_code == 404


def test_app_diagnostics_400_on_bad_id(apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = apps_client.get("/api/app_diagnostics/not a valid id", cookies=cookies)
    assert resp.status_code == 400


def test_app_diagnostics_returns_bundle(cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert body["app_id"] == app_id
    assert body["name"] == "myapp"
    assert body["version"] == "2.3.4"
    assert body["runtime_type"] == "serverfull"
    assert body["status"] == "running"
    # Self-contained: carries system + openhost info too.
    assert body["system"]["python_version"]
    assert "container_runtime" in body
    assert "openhost" in body


def test_app_diagnostics_download_header(cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp")
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}?download=1", cookies=cookies)
    assert resp.status_code == 200
    disp = resp.headers.get("content-disposition", "")
    assert "attachment" in disp
    assert "myapp" in disp
