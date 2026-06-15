"""Config tests for the additive, opt-in cert_api provider.

The BYO-ACME path must stay the default and old configs (written before these
fields existed) must keep loading unchanged.
"""

from __future__ import annotations

from pathlib import Path

import typed_settings

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig


def test_default_provider_is_acme() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    # The broker URL defaults to a host but is only used by the cert_api
    # provider, so the default acme path is unaffected by it.
    # TODO: revert to "https://api.selfhost.imbue.com" once the broker is deployed.
    assert cfg.cert_api_base_url == "https://openhost-cert-api.openhost-qa.selfhost.imbue.com/"
    # Keycloak auth is injected per instance — no defaults.
    assert cfg.cert_api_keycloak_issuer_url is None
    assert cfg.cert_api_keycloak_client_id is None
    assert cfg.cert_api_keycloak_client_secret is None


def test_legacy_config_without_cert_fields_still_loads(tmp_path: Path) -> None:
    # A config exactly as ansible wrote it before this feature existed.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "tls_enabled = true\n"
        "acquire_tls_cert_if_missing = true\n"
        'acme_account_key_path = "/secrets/certbot_private_key.json"\n'
        "coredns_enabled = true\n"
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    assert cfg.acme_account_key_path == "/secrets/certbot_private_key.json"


def test_cert_api_provider_config_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "tls_enabled = true\n"
        "acquire_tls_cert_if_missing = true\n"
        "coredns_enabled = true\n"
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://keycloak.example.com/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "instance-alice"\n'
        'cert_api_keycloak_client_secret = "s3cr3t"\n'
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API
    assert cfg.cert_api_base_url == "https://cert-api.example.com"
    assert cfg.cert_api_keycloak_issuer_url == "https://keycloak.example.com/realms/openhost-customers"
    assert cfg.cert_api_keycloak_client_id == "instance-alice"
    assert cfg.cert_api_keycloak_client_secret == "s3cr3t"


def test_cert_provider_round_trips_through_toml() -> None:
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-alice",
        cert_api_keycloak_client_secret="s3cr3t",
    )
    rendered = cfg.to_toml_str()
    assert 'cert_provider = "cert_api"' in rendered
    assert 'cert_api_base_url = "https://cert-api.example.com"' in rendered
    assert 'cert_api_keycloak_issuer_url = "https://keycloak.example.com/realms/openhost-customers"' in rendered
    assert 'cert_api_keycloak_client_id = "instance-alice"' in rendered
    assert 'cert_api_keycloak_client_secret = "s3cr3t"' in rendered
