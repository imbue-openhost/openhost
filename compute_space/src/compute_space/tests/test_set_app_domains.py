"""Tests for the ``/set_app_domains/<app_id>`` route (alternate/custom domains for an app)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

import compute_space.web
from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.pages.apps import pages_apps_routes

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import make_test_app
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("set-domains"), port=20970)
    init_db(c.db_path)
    return c


def _seed_app(cfg: Any, name: str, local_port: int, alt_domains: list[str] | None = None) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, '1.0', '/tmp/none', ?, 'stopped')""",
            (app_id, name, local_port),
        )
        for domain in alt_domains or []:
            db.execute("INSERT INTO app_alt_domains (app_id, domain) VALUES (?, ?)", (app_id, domain))
        db.commit()
    finally:
        db.close()
    return app_id


def _stored_domains(cfg: Any, app_id: str) -> list[str]:
    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT domain FROM app_alt_domains WHERE app_id = ? ORDER BY domain", (app_id,)).fetchall()
        return [r[0] for r in rows]
    finally:
        db.close()


def test_set_domains_persists_and_normalizes(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(
            f"/set_app_domains/{app_id}",
            json={"domains": [" MyApp.Example.com ", "b.example.com.", ""]},
            cookies=cookies,
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "alt_domains": ["myapp.example.com", "b.example.com"]}
    assert _stored_domains(cfg, app_id) == ["b.example.com", "myapp.example.com"]


def test_set_domains_replaces_existing_set(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971, alt_domains=["old.example.com"])
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": ["new.example.com"]}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert _stored_domains(cfg, app_id) == ["new.example.com"]


def test_set_domains_empty_list_clears(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971, alt_domains=["old.example.com"])
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": []}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert _stored_domains(cfg, app_id) == []


@pytest.mark.parametrize(
    "bad",
    [
        "no-tld",
        "has space.example.com",
        "-leading.example.com",
        "trailing-.example.com",
        "under_score.example.com",
        "double..dot.example.com",
        "a" * 254 + ".com",
    ],
)
def test_set_domains_rejects_invalid(cfg: Any, bad: str) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": [bad]}, cookies=cookies)
    assert r.status_code == 400, bad
    assert _stored_domains(cfg, app_id) == []


def test_set_domains_rejects_zone_and_zone_subdomains(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    cookies = auth_cookie(cfg)
    zone = cfg.zone_domain_no_port
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        for domain in (zone, f"other.{zone}"):
            r = client.post(f"/set_app_domains/{app_id}", json={"domains": [domain]}, cookies=cookies)
            assert r.status_code == 400, domain


def test_set_domains_dedupes_within_request(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(
            f"/set_app_domains/{app_id}",
            json={"domains": ["a.example.com", "A.example.COM."]},
            cookies=cookies,
        )
    assert r.status_code == 200, r.text
    assert _stored_domains(cfg, app_id) == ["a.example.com"]


def test_set_domains_rejects_cross_app_duplicate(cfg: Any) -> None:
    _seed_app(cfg, "appa", 20971, alt_domains=["taken.example.com"])
    app_id_b = _seed_app(cfg, "appb", 20972, alt_domains=["mine.example.com"])
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(
            f"/set_app_domains/{app_id_b}",
            json={"domains": ["taken.example.com"]},
            cookies=cookies,
        )
    assert r.status_code == 409, r.text
    assert "taken.example.com" in r.json()["error"]
    # B's previous set is untouched by the failed replace.
    assert _stored_domains(cfg, app_id_b) == ["mine.example.com"]


def test_set_domains_max_count(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    cookies = auth_cookie(cfg)
    domains = [f"d{i}.example.com" for i in range(21)]
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": domains}, cookies=cookies)
    assert r.status_code == 400


def test_set_domains_removing_app_409(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET status = 'removing' WHERE app_id = ?", (app_id,))
    db.commit()
    db.close()
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": ["a.example.com"]}, cookies=cookies)
    assert r.status_code == 409


def test_set_domains_unknown_and_invalid_app_id(cfg: Any) -> None:
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{new_app_id()}", json={"domains": []}, cookies=cookies)
        assert r.status_code == 404
        r = client.post("/set_app_domains/not-an-id!", json={"domains": []}, cookies=cookies)
        assert r.status_code == 400


def test_set_domains_requires_owner_auth(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20971)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        r = client.post(f"/set_app_domains/{app_id}", json={"domains": ["a.example.com"]})
    assert r.status_code in (401, 302, 307)
    assert _stored_domains(cfg, app_id) == []


def _make_pages_app(cfg: Any) -> Litestar:
    """Litestar app that can render the real app_detail template (with globals installed)."""
    web_dir = Path(compute_space.web.__file__).parent

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        assert isinstance(engine, JinjaTemplateEngine)
        engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[pages_apps_routes],
        template_config=TemplateConfig(directory=web_dir / "templates", engine=JinjaTemplateEngine),
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def test_app_detail_page_shows_domains_and_cname_hint(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20971, alt_domains=["myapp.example.com"])
    cookies = auth_cookie(cfg)
    with TestClient(app=_make_pages_app(cfg)) as client:
        r = client.get("/app_detail/myapp", cookies=cookies)
    assert r.status_code == 200, r.text
    assert "myapp.example.com" in r.text
    assert f"CNAME &rarr; myapp.{cfg.zone_domain_no_port}" in r.text
