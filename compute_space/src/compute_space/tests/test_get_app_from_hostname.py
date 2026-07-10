"""Unit tests for hostname -> app resolution, including alternate (custom) domains."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from compute_space.core.app_id import new_app_id
from compute_space.core.apps import find_app_by_alt_domain
from compute_space.core.apps import get_app_from_hostname
from compute_space.db.connection import init_db

from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("hostname"), port=20950)
    init_db(c.db_path)
    return c


def _seed_app(cfg: Any, name: str, local_port: int, alt_domains: list[str] | None = None) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, '1.0', '/tmp/none', ?, 'running')""",
            (app_id, name, local_port),
        )
        for domain in alt_domains or []:
            db.execute("INSERT INTO app_alt_domains (app_id, domain) VALUES (?, ?)", (app_id, domain))
        db.commit()
    finally:
        db.close()
    return app_id


def test_zone_domain_itself_matches_nothing(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20951)
    assert get_app_from_hostname(cfg.zone_domain) is None


def test_app_subdomain_matches_by_name(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20951)
    app = get_app_from_hostname(f"myapp.{cfg.zone_domain}")
    assert app is not None and app.app_id == app_id


def test_app_subdomain_with_port_and_case(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20951)
    app = get_app_from_hostname(f"MyApp.{cfg.zone_domain}:8443")
    assert app is not None and app.app_id == app_id


def test_multi_label_subdomain_matches_nothing(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20951)
    assert get_app_from_hostname(f"extra.myapp.{cfg.zone_domain}") is None


def test_unknown_subdomain_matches_nothing(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20951)
    assert get_app_from_hostname(f"other.{cfg.zone_domain}") is None


def test_alt_domain_matches(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20951, alt_domains=["myapp.example.com"])
    app = get_app_from_hostname("myapp.example.com")
    assert app is not None and app.app_id == app_id


def test_alt_domain_normalizes_case_port_and_trailing_dot(cfg: Any) -> None:
    app_id = _seed_app(cfg, "myapp", 20951, alt_domains=["myapp.example.com"])
    for host in ("MyApp.Example.COM", "myapp.example.com:443", "myapp.example.com."):
        app = get_app_from_hostname(host)
        assert app is not None and app.app_id == app_id, host


def test_alt_domain_routes_to_owning_app(cfg: Any) -> None:
    _seed_app(cfg, "appa", 20951, alt_domains=["a.example.com"])
    app_id_b = _seed_app(cfg, "appb", 20952, alt_domains=["b.example.com"])
    app = get_app_from_hostname("b.example.com")
    assert app is not None and app.app_id == app_id_b


def test_unregistered_external_host_matches_nothing(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20951, alt_domains=["myapp.example.com"])
    assert get_app_from_hostname("other.example.com") is None
    assert get_app_from_hostname("localhost:8080") is None


def test_alt_domain_is_exact_match_not_suffix(cfg: Any) -> None:
    _seed_app(cfg, "myapp", 20951, alt_domains=["myapp.example.com"])
    assert get_app_from_hostname("sub.myapp.example.com") is None
    assert find_app_by_alt_domain("sub.myapp.example.com") is None
