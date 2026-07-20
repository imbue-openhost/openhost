"""Tests for email provisioning: proxy client parsing + zone-record publishing."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from compute_space.config import DefaultConfig
from compute_space.core.email.provision import provision_email_records
from compute_space.core.email.proxy_client import EmailProxyClient
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.tls.keycloak import StaticTokenProvider


def _write_zonefile(path: Path, serial: int = 100) -> None:
    path.write_text(
        "$ORIGIN alice.example.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.alice.example.com. admin.alice.example.com. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.alice.example.com.\n"
        "@   IN A    127.0.0.1\n"
    )


def _client_with_handler(handler) -> EmailProxyClient:
    transport = httpx.MockTransport(handler)
    return EmailProxyClient(
        base_url="https://proxy.test",
        token_provider=StaticTokenProvider(token="test-token"),
        http_client=httpx.Client(transport=transport),
    )


def test_proxy_client_parses_identity_and_sends_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "domain": "alice.example.com",
                "verified": False,
                "dkim_records": [
                    {"name": "a._domainkey.alice.example.com", "value": "a.dkim.amazonses.com"},
                    {"name": "b._domainkey.alice.example.com", "value": "b.dkim.amazonses.com"},
                ],
            },
        )

    client = _client_with_handler(handler)
    result = client.ensure_identity()
    assert seen["auth"] == "Bearer test-token"
    assert seen["path"] == "/v1/identity"
    assert result.domain == "alice.example.com"
    assert len(result.dkim_records) == 2
    assert result.dkim_records[0].name == "a._domainkey.alice.example.com"


def test_proxy_client_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream boom")

    client = _client_with_handler(handler)
    with pytest.raises(EmailProxyError, match="HTTP 502"):
        client.ensure_identity()


def test_provision_email_records_writes_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    )
    # Point the config's zonefile path at our temp file.
    monkeypatch.setattr(
        type(cfg), "coredns_zonefile_path", property(lambda self: zonefile)
    )

    # Stub the proxy client construction to return a fake identity.
    import compute_space.core.email.provision as prov

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            from compute_space.core.email.proxy_client import DkimRecord, IdentityResult

            return IdentityResult(
                domain="alice.example.com",
                verified=False,
                dkim_records=(DkimRecord(name="tok._domainkey.alice.example.com", value="tok.dkim.amazonses.com"),),
            )

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FakeClient()))

    provision_email_records(cfg)

    content = zonefile.read_text()
    assert "v=spf1 include:amazonses.com" in content
    assert "v=DMARC1" in content
    assert "@   IN MX   10 inbound-smtp.us-west-2.amazonaws.com." in content
    assert "tok._domainkey.alice.example.com.   IN CNAME  tok.dkim.amazonses.com." in content


def test_provision_email_records_noop_when_disabled(tmp_path: Path):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    before = zonefile.read_text()
    cfg = DefaultConfig(zone_domain="alice.example.com")  # email disabled
    provision_email_records(cfg)
    assert zonefile.read_text() == before


def test_provision_email_records_survives_proxy_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    )
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))

    import compute_space.core.email.provision as prov

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            raise EmailProxyError("proxy down")

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FailingClient()))

    # Must not raise, and must not modify the zone (fail-open).
    provision_email_records(cfg)
    assert zonefile.read_text() == before
