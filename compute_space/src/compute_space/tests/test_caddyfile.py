"""Tests for Caddyfile generation, including on-demand TLS for custom domains."""

from __future__ import annotations

import datetime
import shutil
import subprocess
from typing import Any

import pytest

from compute_space.core.caddy import generate_caddyfile

from .conftest import _make_test_config
from .test_cert_renewal import _write_self_signed_cert


@pytest.fixture
def tls_cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return _make_test_config(
        tmp_path_factory.mktemp("caddyfile"),
        port=20980,
        tls_enabled=True,
        zone_domain="zone.example.org",
    )


def test_tls_caddyfile_zone_site_uses_static_cert(tls_cfg: Any) -> None:
    caddyfile = generate_caddyfile(tls_cfg)
    assert "https://zone.example.org, https://*.zone.example.org {" in caddyfile
    assert f"tls {tls_cfg.tls_cert_path} {tls_cfg.tls_key_path}" in caddyfile


def test_tls_caddyfile_on_demand_for_custom_domains(tls_cfg: Any) -> None:
    caddyfile = generate_caddyfile(tls_cfg)
    assert "on_demand_tls {" in caddyfile
    assert "ask http://127.0.0.1:20980/api/tls/on_demand_check" in caddyfile
    # Catch-all https:// site issues certs on demand for registered domains.
    assert "https:// {" in caddyfile
    assert "on_demand" in caddyfile.split("https:// {")[1]
    # ACME machinery must stay enabled (auto_https off would break on-demand);
    # only Caddy's automatic :80 redirect site is suppressed.
    assert "auto_https disable_redirects" in caddyfile
    assert "auto_https off" not in caddyfile
    assert f"root {tls_cfg.caddy_storage_dir}" in caddyfile


def test_tls_caddyfile_http_redirect(tls_cfg: Any) -> None:
    """When TLS is enabled, plain HTTP redirects to HTTPS (and does not proxy)."""
    caddyfile = generate_caddyfile(tls_cfg)
    assert "http:// {" in caddyfile
    assert "redir https://{host}{uri} permanent" in caddyfile
    lines_in_http_block = caddyfile.split("http:// {")[1].split("}")[0]
    assert "reverse_proxy" not in lines_in_http_block


def test_tls_caddyfile_email_only_when_configured(tmp_path_factory: pytest.TempPathFactory) -> None:
    no_email = _make_test_config(tmp_path_factory.mktemp("caddyfile-nomail"), port=20981, tls_enabled=True)
    assert "\n    email " not in generate_caddyfile(no_email)
    with_email = _make_test_config(
        tmp_path_factory.mktemp("caddyfile-email"), port=20982, tls_enabled=True, acme_email="owner@example.com"
    )
    assert "    email owner@example.com\n" in generate_caddyfile(with_email)


def test_non_tls_caddyfile_plain_proxy(tmp_path_factory: pytest.TempPathFactory) -> None:
    cfg = _make_test_config(tmp_path_factory.mktemp("caddyfile-plain"), port=20983, tls_enabled=False)
    caddyfile = generate_caddyfile(cfg)
    assert "auto_https off" in caddyfile
    assert ":80 {" in caddyfile
    assert "reverse_proxy localhost:20983" in caddyfile
    assert "on_demand" not in caddyfile


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy binary not available")
def test_tls_caddyfile_validates(tls_cfg: Any) -> None:
    """The generated config must be accepted by ``caddy validate``."""
    _write_self_signed_cert(
        tls_cfg.tls_cert_path,
        tls_cfg.tls_key_path,
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
    )
    caddyfile_path = tls_cfg.caddyfile_path
    caddyfile_path.write_text(generate_caddyfile(tls_cfg))
    result = subprocess.run(
        ["caddy", "validate", "--config", str(caddyfile_path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
