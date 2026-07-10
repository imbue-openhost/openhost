"""Tests for the Caddy on-demand TLS "ask" endpoint."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db
from compute_space.web.routes.api.tls import api_tls_routes

from ._litestar_helpers import make_test_app
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("tls-check"), port=20960)
    init_db(c.db_path)
    return c


def _seed_app_with_domain(cfg: Any, domain: str) -> None:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, 'myapp', '1.0', '/tmp/none', 20961, 'running')""",
            (app_id,),
        )
        db.execute("INSERT INTO app_alt_domains (app_id, domain) VALUES (?, ?)", (app_id, domain))
        db.commit()
    finally:
        db.close()


def test_registered_domain_allowed(cfg: Any) -> None:
    _seed_app_with_domain(cfg, "myapp.example.com")
    with TestClient(app=make_test_app(api_tls_routes)) as client:
        # No auth cookie: Caddy calls this endpoint before any session exists.
        r = client.get("/api/tls/on_demand_check", params={"domain": "myapp.example.com"})
        assert r.status_code == 200


def test_registered_domain_normalizes_case_and_trailing_dot(cfg: Any) -> None:
    _seed_app_with_domain(cfg, "myapp.example.com")
    with TestClient(app=make_test_app(api_tls_routes)) as client:
        for domain in ("MyApp.Example.COM", "myapp.example.com."):
            r = client.get("/api/tls/on_demand_check", params={"domain": domain})
            assert r.status_code == 200, domain


def test_unregistered_domain_refused(cfg: Any) -> None:
    _seed_app_with_domain(cfg, "myapp.example.com")
    with TestClient(app=make_test_app(api_tls_routes)) as client:
        r = client.get("/api/tls/on_demand_check", params={"domain": "other.example.com"})
        assert r.status_code == 404


def test_missing_domain_param_rejected(cfg: Any) -> None:
    with TestClient(app=make_test_app(api_tls_routes)) as client:
        r = client.get("/api/tls/on_demand_check")
        assert r.status_code == 400
