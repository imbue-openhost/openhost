from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _login_required_redirect
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.federation import federation_connect

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config

SPEC_URL = "https://github.com/imbue-openhost/md-notes/blob/main/docs/federation.md"

FEDERATED_MANIFEST = f"""\
[app]
name = "md-notes"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[federation]
url = "{SPEC_URL}"
"""

PLAIN_MANIFEST = """\
[app]
name = "plain-app"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any) -> Litestar:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[federation_connect],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        exception_handlers={NotAuthorizedException: _login_required_redirect},
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _seed_app(db_path: str, name: str, local_port: int, manifest_raw: str | None) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)"
            " VALUES (?, ?, '0.1.0', ?, ?, 'running', ?)",
            (f"{name}appid"[:12], name, f"/tmp/{name}", local_port, manifest_raw),
        )
        conn.commit()
    finally:
        conn.close()


INVITE_PARAMS = {
    "spec": SPEC_URL,
    "source": "https://md-notes.usera.example.com",
    "vault": "my vault",
    "secret": "s3cr3t",
}


def test_lists_matching_app_with_passthrough_query(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_app(cfg.db_path, "notes", 19010, FEDERATED_MANIFEST)
    _seed_app(cfg.db_path, "plain", 19011, PLAIN_MANIFEST)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=INVITE_PARAMS, cookies=cookie)
    assert resp.status_code == 200
    assert "Connect with notes" in resp.text
    # Inviter shown as a hostname, spec rendered as a link.
    assert "md-notes.usera.example.com" in resp.text
    assert SPEC_URL in resp.text
    # Non-federated app is not offered.
    assert "Connect with plain" not in resp.text

    # The connect link points at the app subdomain + connect path, with the
    # original query string passed through verbatim.
    hrefs = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', resp.text)]
    connect_links = [h for h in hrefs if h.startswith(f"http://notes.{cfg.zone_domain}")]
    assert len(connect_links) == 1
    parsed = urlparse(connect_links[0])
    assert parsed.path == "/federation/connect"
    assert parse_qs(parsed.query) == {k: [v] for k, v in INVITE_PARAMS.items()}


def test_custom_connect_path_is_used(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    manifest = FEDERATED_MANIFEST + 'connect_path = "/fed/join"\n'
    _seed_app(cfg.db_path, "notes", 19010, manifest)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=INVITE_PARAMS, cookies=cookie)
    assert resp.status_code == 200
    assert f"http://notes.{cfg.zone_domain}/fed/join?" in html.unescape(resp.text)


def test_no_matching_app_shows_message(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_app(cfg.db_path, "plain", 19011, PLAIN_MANIFEST)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=INVITE_PARAMS, cookies=cookie)
    assert resp.status_code == 200
    assert "No installed app serves this protocol" in resp.text
    assert "Connect with" not in resp.text
    # No catalog installed -> no catalog link.
    assert "catalog" not in resp.text


def test_no_matching_app_links_to_catalog_when_installed(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_app(cfg.db_path, "catalog", 19012, None)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=INVITE_PARAMS, cookies=cookie)
    assert resp.status_code == 200
    assert "No installed app serves this protocol" in resp.text
    hrefs = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', resp.text)]
    catalog_links = [h for h in hrefs if h.startswith(f"http://catalog.{cfg.zone_domain}/")]
    assert len(catalog_links) == 1
    assert parse_qs(urlparse(catalog_links[0]).query) == {"federation_url": [SPEC_URL]}


def test_missing_spec_is_rejected(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params={"source": "https://a.example.com"}, cookies=cookie)
    assert resp.status_code == 400


def test_unauthenticated_redirects_to_login(cfg: Any) -> None:
    set_active_config(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=INVITE_PARAMS, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]
    assert "Federation invite" not in resp.text


def test_non_http_spec_is_not_linkified(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_app(cfg.db_path, "plain", 19011, PLAIN_MANIFEST)

    params = dict(INVITE_PARAMS, spec="javascript:alert(1)")
    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/redirect/federation/connect", params=params, cookies=cookie)
    assert resp.status_code == 200
    assert 'href="javascript:' not in resp.text
    assert "javascript:alert(1)" in resp.text  # still shown as text
