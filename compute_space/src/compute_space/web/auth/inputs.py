"""Build framework-neutral ``AuthInputs`` from a Litestar connection."""

from typing import Any

from litestar.connection import ASGIConnection
from litestar.enums import ScopeType

from compute_space.core.auth.inputs import AuthInputs


def auth_inputs_from_connection(connection: ASGIConnection[Any, Any, Any, Any]) -> AuthInputs:
    cookies = dict(connection.cookies)
    cookie_header = connection.headers.get("Cookie", "")
    auth_header = connection.headers.get("Authorization", "")
    if connection.scope["type"] == ScopeType.WEBSOCKET:
        method = "WS"
    else:
        method = str(connection.scope.get("method") or "GET")
    return AuthInputs(
        cookies=cookies,
        cookie_header=cookie_header,
        auth_header=auth_header,
        method=method,
        path=connection.scope.get("path", "/"),
    )
