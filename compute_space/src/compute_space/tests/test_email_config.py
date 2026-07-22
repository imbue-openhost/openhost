"""Config tests for the additive, opt-in email feature.

Email is disabled by default; enabling it requires the proxy URL, per-instance
Keycloak client-credentials, and the inbound MX host. Old configs (written
before these fields existed) must keep loading unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings

from compute_space.config import DefaultConfig


def _full_email_kwargs() -> dict[str, object]:
    return dict(
        email_enabled=True,
        email_proxy_base_url="https://openhost-email-proxy.fly.dev",
        email_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    )


def test_email_disabled_by_default() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_enabled is False
    assert cfg.email_proxy_base_url is None
    assert cfg.email_keycloak_client_secret is None
    assert cfg.email_inbound_mx_host is None


def test_email_enabled_requires_all_fields() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    with pytest.raises(ValueError, match="email_proxy_base_url must be set"):
        cfg.evolve(email_enabled=True)
    # Missing just the MX host still fails.
    partial = {k: v for k, v in _full_email_kwargs().items() if k != "email_inbound_mx_host"}
    with pytest.raises(ValueError, match="email_inbound_mx_host must be set"):
        cfg.evolve(**partial)


def test_email_enabled_with_all_fields_ok() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    assert cfg.email_enabled is True
    assert cfg.email_inbound_mx_host == "inbound-smtp.us-west-2.amazonaws.com"


def test_legacy_config_without_email_fields_still_loads(tmp_path: Path) -> None:
    # A config exactly as ansible wrote it before the email feature existed.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "legacy.example.com"\n'
        "tls_enabled = true\n"
        "acquire_tls_cert_if_missing = true\n"
        'acme_account_key_path = "/secrets/certbot_private_key.json"\n'
        "coredns_enabled = true\n"
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.email_enabled is False


def test_email_config_round_trips_through_toml() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    rendered = cfg.to_toml_str()
    assert "email_enabled = true" in rendered
    assert 'email_proxy_base_url = "https://openhost-email-proxy.fly.dev"' in rendered
    assert 'email_keycloak_client_id = "instance-alice"' in rendered
    assert 'email_inbound_mx_host = "inbound-smtp.us-west-2.amazonaws.com"' in rendered


def test_email_smtp_relay_fields_round_trip() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(
        **_full_email_kwargs(),
        email_smtp_relay_host="openhost-email-proxy.internal",
        email_smtp_relay_port=587,
        email_smtp_relay_user="x.example.com",
        email_smtp_relay_password="hmac-pw",
    )
    rendered = cfg.to_toml_str()
    assert 'email_smtp_relay_host = "openhost-email-proxy.internal"' in rendered
    assert "email_smtp_relay_port = 587" in rendered
    assert 'email_smtp_relay_user = "x.example.com"' in rendered
    assert 'email_smtp_relay_password = "hmac-pw"' in rendered
