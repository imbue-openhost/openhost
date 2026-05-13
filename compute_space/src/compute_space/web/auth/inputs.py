"""Build framework-neutral ``AuthInputs`` from a Litestar connection."""

from typing import Any

from litestar.connection import ASGIConnection

from compute_space.core.auth.inputs import AuthInputs


def auth_inputs_from_connection(connection: ASGIConnection[Any, Any, Any, Any]) -> AuthInputs:
    """Build an ``AuthInputs`` from a Litestar request or websocket connection."""
    cookie_header = ""
    for key, value in connection.scope.get("headers", []):
        if key.lower() == b"cookie":
            cookie_header = value.decode("latin-1")
            break
    raw_method = connection.scope.get("method")
    if raw_method is not None:
        method = str(raw_method)
    else:
        method = "WS" if str(connection.scope.get("type", "")) == "websocket" else "GET"
    return AuthInputs(
        cookies=dict(connection.cookies),
        cookie_header=cookie_header,
        auth_header=connection.headers.get("Authorization", ""),
        method=method,
        path=connection.scope.get("path", "/"),
    )
