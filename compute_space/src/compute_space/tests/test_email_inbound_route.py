"""Tests for the router's /_email/inbound endpoint.

The email proxy POSTs received mail here (via the imbue-hosted-spaces public
door). The router authenticates the hop with the per-instance SMTP relay
password (HMAC of the zone) it already holds, then forwards to the mailbox app's
own /_email/inbound on its loopback port. These tests cover the auth gate
(unconfigured, missing/bad/good token), the no-mailbox-app case, and that a good
request forwards to the resolved app port.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.system import system_routes

RELAY_PW = "a" * 64  # stand-in HMAC-SHA256 hex

# Inbound auth now verifies the token against the runtime-fetched relay credential
# (no secret in config). Patch the provider's verify to accept exactly RELAY_PW.
_VERIFY = "compute_space.core.email.relay_credential.RelayCredentialProvider.verify_inbound_token"


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    config = _make_test_config(
        tmp_path,
        email_enabled=True,
        email_mailbox_app_names=["stalwart-email-server"],
        email_proxy_base_url="https://frontend.example",
        email_keycloak_issuer_url="https://kc.example/realms/openhost-customers",
        email_keycloak_client_id="instance-x",
        email_keycloak_client_secret="secret",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    )
    init_db(config.db_path)
    app = Litestar(
        route_handlers=[system_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
    )
    with TestClient(app=app) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_token_401(client: TestClient) -> None:
    with mock.patch(_VERIFY, lambda self, t: t == RELAY_PW):
        resp = client.post("/_email/inbound", content=b"raw")
    assert resp.status_code == 401


def test_bad_token_401(client: TestClient) -> None:
    with mock.patch(_VERIFY, lambda self, t: t == RELAY_PW):
        resp = client.post("/_email/inbound", content=b"raw", headers=_auth("b" * 64))
    assert resp.status_code == 401


def test_good_token_no_mailbox_app_503(client: TestClient) -> None:
    # find_app_by_name returns None (no app deployed) -> 503.
    with (
        mock.patch(_VERIFY, lambda self, t: t == RELAY_PW),
        mock.patch("compute_space.web.routes.api.system.find_app_by_name", return_value=None),
    ):
        resp = client.post("/_email/inbound", content=b"raw", headers=_auth(RELAY_PW))
    assert resp.status_code == 503


def test_good_token_forwards_to_mailbox_app(client: TestClient) -> None:
    fake_app = mock.Mock(local_port=19042)

    async def fake_proxy(request: Any, target_port: int, **kwargs: Any) -> ASGIResponse:
        assert target_port == 19042
        assert kwargs["override_path"] == "/_email/inbound"
        return ASGIResponse(body=b'{"delivered":true}', status_code=200)

    with (
        mock.patch(_VERIFY, lambda self, t: t == RELAY_PW),
        mock.patch("compute_space.web.routes.api.system.find_app_by_name", return_value=fake_app),
        mock.patch(
            "compute_space.web.routes.api.system.proxy_http_request", side_effect=fake_proxy
        ) as proxied,
    ):
        resp = client.post("/_email/inbound", content=b"raw rfc822", headers=_auth(RELAY_PW))

    assert resp.status_code == 200
    assert proxied.called


def test_unconfigured_instance_401(tmp_path: Path) -> None:
    # email disabled -> 401 (does not leak that it's unconfigured vs bad creds).
    config = _make_test_config(tmp_path, email_enabled=False)
    init_db(config.db_path)
    app = Litestar(
        route_handlers=[system_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
    )
    with TestClient(app=app) as c:
        resp = c.post("/_email/inbound", content=b"raw", headers=_auth(RELAY_PW))
    assert resp.status_code == 401
