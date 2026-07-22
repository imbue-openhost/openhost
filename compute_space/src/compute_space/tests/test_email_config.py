"""Config tests for the additive, opt-in email feature.

Email is disabled by default; enabling it requires the proxy URL, per-instance
Keycloak client-credentials, and the inbound MX host. Old configs (written
before these fields existed) must keep loading unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings
from litestar.exceptions import NotAuthorizedException

import compute_space.web.routes.api.system as sys_mod
from compute_space.config import DefaultConfig
from compute_space.web.routes.api.system import custom_email_domain


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


def test_custom_domain_none_by_default() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_custom_domain is None
    assert cfg.email_custom_domain_normalized is None
    assert cfg.custom_domain_delegation_record() is None


def test_custom_domain_normalized_lowercases_and_strips_dot() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="Mail.MyDomain.Com.")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_blank_treated_as_unset() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="   ")
    assert cfg.email_custom_domain_normalized is None


def test_custom_domain_delegation_record() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com:8443").evolve(email_custom_domain="mail.mydomain.com")
    rec = cfg.custom_domain_delegation_record()
    assert rec is not None
    assert rec.name == "mail.mydomain.com"
    assert rec.record_type == "NS"
    assert rec.value == "ns.alice.selfhost.imbue.com"
    assert rec.as_display_line() == "mail.mydomain.com   NS   ns.alice.selfhost.imbue.com"


def test_custom_domain_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="not a domain")


def test_custom_domain_rejects_overlap_with_zone() -> None:
    # The built-in zone already handles its own name and subdomains; a custom
    # domain that overlaps would double-declare records.
    with pytest.raises(ValueError, match="overlaps the instance zone"):
        DefaultConfig(zone_domain="alice.example.com").evolve(email_custom_domain="mail.alice.example.com")
    with pytest.raises(ValueError, match="overlaps the instance zone"):
        DefaultConfig(zone_domain="alice.example.com").evolve(email_custom_domain="alice.example.com")


def test_custom_domain_validated_even_when_email_disabled() -> None:
    # A typo should surface at config load, not silently wait until email is on.
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="bad_domain!")


def test_custom_domain_round_trips_through_toml() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(
        **_full_email_kwargs(),
        email_custom_domain="mail.mydomain.com",
    )
    assert 'email_custom_domain = "mail.mydomain.com"' in cfg.to_toml_str()


def test_custom_email_domain_route_returns_record_when_set() -> None:
    # The owner-facing route surfaces the exact NS record to paste at the registrar.

    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(email_custom_domain="mail.mydomain.com")
    resp = custom_email_domain.fn(cfg)  # type: ignore[attr-defined]
    assert resp.configured is True
    assert resp.domain == "mail.mydomain.com"
    assert resp.record_name == "mail.mydomain.com"
    assert resp.record_type == "NS"
    assert resp.record_value == "ns.alice.selfhost.imbue.com"
    assert resp.display_line == "mail.mydomain.com   NS   ns.alice.selfhost.imbue.com"


def test_custom_email_domain_route_reports_unconfigured() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")  # no custom domain
    resp = custom_email_domain.fn(cfg)  # type: ignore[attr-defined]
    assert resp.configured is False
    assert resp.domain is None
    assert resp.display_line is None


def test_mailbox_app_names_default() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")
    assert cfg.email_mailbox_app_names == ["stalwart-email-server"]


class _FakeDB:
    def __init__(self, app_name: str | None) -> None:
        self._app_name = app_name

    def execute(self, *_args: object) -> _FakeDB:
        return self

    def fetchone(self) -> dict[str, str] | None:
        return {"name": self._app_name} if self._app_name is not None else None


def test_relay_config_rejects_non_mailbox_app(monkeypatch) -> None:

    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-123")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_full_email_kwargs())
    # A different app (not in email_mailbox_app_names) must be refused.
    with pytest.raises(NotAuthorizedException):
        sys_mod.email_relay_config.fn(object(), _FakeDB("some-other-app"), cfg)  # type: ignore[attr-defined]


def test_relay_config_returns_creds_to_mailbox_app(monkeypatch) -> None:
    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-mail")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_full_email_kwargs(),
        email_smtp_relay_host="openhost-email-proxy.internal",
        email_smtp_relay_port=587,
        email_smtp_relay_user="alice.selfhost.imbue.com",
        email_smtp_relay_password="hmac-pw",
        email_custom_domain="mail.mydomain.com",
    )
    resp = sys_mod.email_relay_config.fn(object(), _FakeDB("stalwart-email-server"), cfg)  # type: ignore[attr-defined]
    body = resp.content
    assert body.configured is True
    assert body.smtp_relay_host == "openhost-email-proxy.internal"
    assert body.smtp_relay_password == "hmac-pw"
    assert body.zone_domain == "alice.selfhost.imbue.com"
    assert body.custom_domain == "mail.mydomain.com"


def test_relay_config_reports_unconfigured_when_email_off(monkeypatch) -> None:
    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-mail")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")  # email disabled
    resp = sys_mod.email_relay_config.fn(object(), _FakeDB("stalwart-email-server"), cfg)  # type: ignore[attr-defined]
    assert resp.content.configured is False
    assert resp.content.smtp_relay_password is None
