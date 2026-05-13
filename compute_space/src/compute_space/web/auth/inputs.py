"""Quart adapters that build framework-neutral ``AuthInputs`` from a Quart request/websocket."""

from quart.wrappers import Request
from quart.wrappers import Websocket

from compute_space.core.auth.inputs import AuthInputs


def auth_inputs_from_request(req_or_ws: Request | Websocket) -> AuthInputs:
    """Build an ``AuthInputs`` from a Quart Request or Websocket."""
    return AuthInputs(
        cookies=dict(req_or_ws.cookies),
        cookie_header=req_or_ws.headers.get("Cookie", ""),
        auth_header=req_or_ws.headers.get("Authorization", ""),
        method=getattr(req_or_ws, "method", "WS"),
        path=req_or_ws.path,
    )
