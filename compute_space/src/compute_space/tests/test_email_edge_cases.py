"""Edge-case suite for the openhost email router endpoint + relay credential provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.email import relay_credential as rc
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialProvider
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.system import system_routes

RELAY_PW = "d" * 64
_VERIFY = "compute_space.core.email.relay_credential.RelayCredentialProvider.verify_inbound_token"
_EMAIL_KW = dict(
    email_enabled=True,
    email_mailbox_app_names=["stalwart-email-server"],
    email_proxy_base_url="https://frontend.example",
    email_keycloak_issuer_url="https://kc.example/realms/openhost-customers",
    email_keycloak_client_id="instance-x",
    email_keycloak_client_secret="secret",
    email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
)


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    config = _make_test_config(tmp_path, **_EMAIL_KW)
    init_db(config.db_path)
    app = Litestar(
        route_handlers=[system_routes],
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
    )
    with TestClient(app=app) as c:
        yield c


# ─────────────────────── /_email/inbound auth header parsing ───────────────────────


@pytest.mark.parametrize(
    "header,should_pass",
    [
        (f"Bearer {RELAY_PW}", True),
        (f"bearer {RELAY_PW}", False),  # scheme is case-sensitive in the endpoint (startswith 'Bearer ')
        (RELAY_PW, False),  # no scheme
        (f"Bearer  {RELAY_PW}", False),  # double space -> token mismatch
        ("Bearer ", False),  # empty token
        ("", False),  # no header
        (f"Basic {RELAY_PW}", False),  # wrong scheme
        (f"Bearer {RELAY_PW}extra", False),  # wrong token
    ],
)
def test_inbound_auth_header_shapes(client, header, should_pass):
    # verify_inbound_token accepts exactly RELAY_PW; the endpoint's own header
    # parsing must extract the token correctly for that to matter.
    with (
        mock.patch(_VERIFY, lambda self, t: t == RELAY_PW),
        mock.patch("compute_space.web.routes.api.system.find_app_by_name", return_value=mock.Mock(local_port=1)),
        mock.patch("compute_space.web.routes.api.system.proxy_http_request", new=_ok_proxy()),
    ):
        headers = {"Authorization": header} if header else {}
        resp = client.post("/_email/inbound", content=b"x", headers=headers)
    if should_pass:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 401


def _ok_proxy():
    async def _p(request, target_port, **kw):
        return ASGIResponse(body=b"{}", status_code=200)

    return _p


def test_inbound_picks_first_deployed_mailbox_app(tmp_path):
    config = _make_test_config(
        tmp_path, **{**_EMAIL_KW, "email_mailbox_app_names": ["missing-app", "stalwart-email-server"]}
    )
    init_db(config.db_path)
    app = Litestar(
        route_handlers=[system_routes],
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
    )

    def fake_find(name):
        return None if name == "missing-app" else mock.Mock(local_port=4242)

    seen = {}

    async def fake_proxy(request, target_port, **kw):
        seen["port"] = target_port
        return ASGIResponse(body=b"{}", status_code=200)

    with (
        TestClient(app=app) as c,
        mock.patch(_VERIFY, lambda self, t: True),
        mock.patch("compute_space.web.routes.api.system.find_app_by_name", side_effect=fake_find),
        mock.patch("compute_space.web.routes.api.system.proxy_http_request", side_effect=fake_proxy),
    ):
        resp = c.post("/_email/inbound", content=b"x", headers={"Authorization": f"Bearer {RELAY_PW}"})
    assert resp.status_code == 200
    assert seen["port"] == 4242  # skipped the missing app, used the deployed one


def test_inbound_get_405_or_404(client):
    # GET on a POST-only route -> 405 (litestar) ; ensure it's not accidentally allowed.
    resp = client.get("/_email/inbound", headers={"Authorization": f"Bearer {RELAY_PW}"})
    assert resp.status_code in (404, 405)


# ─────────────────────── RelayCredentialProvider edge cases ───────────────────────


def _provider(tmp_path, clock):
    config = _make_test_config(tmp_path, zone_domain="alice.selfhost.imbue.com", **_EMAIL_KW)
    return RelayCredentialProvider(config=config, monotonic=lambda: clock[0])


_CRED = RelayCredential(
    smtp_relay_host="h",
    smtp_relay_port=465,
    smtp_relay_user="u",
    smtp_relay_password="pw",
    zone_domain="z",
    custom_domain=None,
)


def test_provider_caches_within_ttl(tmp_path):
    clock = [0.0]
    p = _provider(tmp_path, clock)
    with mock.patch.object(RelayCredentialProvider, "_fetch", return_value=_CRED) as f:
        p.get()
        p.get()
        assert f.call_count == 1
        clock[0] += rc._CACHE_TTL_SECONDS - 1
        p.get()
        assert f.call_count == 1  # still within TTL


def test_provider_refetches_after_ttl(tmp_path):
    clock = [0.0]
    p = _provider(tmp_path, clock)
    with mock.patch.object(RelayCredentialProvider, "_fetch", return_value=_CRED) as f:
        p.get()
        clock[0] += rc._CACHE_TTL_SECONDS + 0.1
        p.get()
        assert f.call_count == 2


def test_provider_disabled_returns_none_no_fetch(tmp_path):
    config = _make_test_config(tmp_path)  # email disabled
    p = RelayCredentialProvider(config=config)
    with mock.patch.object(RelayCredentialProvider, "_fetch") as f:
        assert p.get() is None
        f.assert_not_called()


def test_provider_fetch_http_error_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    with mock.patch("httpx.Client") as C:
        C.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("down")
        with mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"):
            with pytest.raises(rc.RelayCredentialError):
                p._fetch()


def test_provider_fetch_non_200_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=503)
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()


def test_provider_fetch_unconfigured_body_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"configured": False}
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()


def test_provider_fetch_malformed_body_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"configured": True}  # missing required fields
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()


def test_verify_inbound_token_matches(tmp_path):
    p = _provider(tmp_path, [0.0])
    with mock.patch.object(RelayCredentialProvider, "_fetch", return_value=_CRED):
        assert p.verify_inbound_token("pw") is True
        assert p.verify_inbound_token("PW") is False
        assert p.verify_inbound_token("") is False
        assert p.verify_inbound_token("pw ") is False  # exact match only


def test_verify_inbound_token_fails_closed_on_error(tmp_path):
    p = _provider(tmp_path, [0.0])
    with mock.patch.object(RelayCredentialProvider, "_fetch", side_effect=rc.RelayCredentialError("x")):
        assert p.verify_inbound_token("pw") is False


def test_verify_inbound_token_disabled_false(tmp_path):
    config = _make_test_config(tmp_path)
    p = RelayCredentialProvider(config=config)
    assert p.verify_inbound_token("anything") is False
