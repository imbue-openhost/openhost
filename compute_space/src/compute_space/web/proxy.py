"""Compat shim for the legacy ``compute_space.web.proxy`` module location.

The unmigrated ``web/routes/services_v2.py`` blueprint still imports
``proxy_request_quart`` / ``ws_proxy_quart`` from here.  Both wrap the
ASGI-native helpers in ``web.middleware.subdomain_proxy`` so legacy Quart
handlers can keep calling them with Quart Request/Websocket objects.  This
module is meant to disappear once services_v2 is ported to Litestar.
"""

from typing import Any
from typing import cast

from litestar import WebSocket
from litestar.datastructures import Headers
from litestar.response.base import ASGIResponse
from litestar.types import Receive
from litestar.types import Scope
from quart import Response as QuartResponse
from quart.wrappers import Request as QuartRequest
from quart.wrappers import Websocket as QuartWebsocket
from werkzeug.datastructures import Headers as WerkzeugHeaders

from compute_space.web.middleware.subdomain_proxy import proxy_request
from compute_space.web.middleware.subdomain_proxy import ws_proxy


def _asgi_to_quart(proxied: ASGIResponse) -> QuartResponse:
    """Convert a Litestar ASGIResponse (returned by ``proxy_request``) into a QuartResponse."""
    headers = WerkzeugHeaders()
    media_type: str | None = None
    for key_b, value_b in proxied.encoded_headers:
        key = key_b.decode("latin-1")
        lower = key.lower()
        if lower == "content-length":
            # Quart computes content-length from the body itself.
            continue
        if lower == "content-type":
            media_type = value_b.decode("latin-1")
            continue
        headers.add(key, value_b.decode("latin-1"))
    body = proxied.body if isinstance(proxied.body, bytes) else proxied.body.encode("utf-8")
    return QuartResponse(body, status=proxied.status_code, headers=headers, content_type=media_type)


def _scope_with_filtered_headers(scope: Scope, drop_keys: set[str]) -> Scope:
    """Return a shallow scope copy with ``drop_keys`` removed from ``headers``.

    ``proxy_request`` reads the request headers from ``scope["headers"]``;
    services_v2 historically relied on being able to pass ``Header=None`` to
    suppress a header (notably stripping the consumer's Authorization cookie
    before forwarding to a provider).  We honour that by pruning the scope
    headers up-front rather than extending the ASGI helper's API.
    """
    if not drop_keys:
        return scope
    dropped_lower = {k.lower().encode("latin-1") for k in drop_keys}
    new_headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() not in dropped_lower]
    new_scope = dict(scope)
    new_scope["headers"] = new_headers
    return cast(Scope, new_scope)


async def proxy_request_quart(
    quart_request: QuartRequest,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
) -> QuartResponse:
    """Quart-flavored wrapper around ``proxy_request``.

    Quart has already consumed the ASGI ``receive`` callable, so we buffer the
    request body and synthesize a one-shot receive for the ASGI helper.  Any
    header in ``extra_headers`` whose value is ``None`` is treated as "drop
    this header from the forwarded request" (the new ASGI helper only knows
    how to append, so the filtering happens here).
    """
    body = await quart_request.get_data()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    drop_keys: set[str] = set()
    append_headers: list[tuple[str, str]] = []
    if extra_headers:
        for k, v in extra_headers.items():
            if v is None:
                drop_keys.add(k)
            else:
                append_headers.append((k, v))

    scope = _scope_with_filtered_headers(cast(Scope, quart_request.scope), drop_keys)
    # Ensure that even if the legacy caller didn't pass Header=None, any
    # already-present copy of the same key gets replaced rather than duplicated.
    if append_headers:
        scope = _scope_with_filtered_headers(scope, {k for k, _ in append_headers})

    proxied = await proxy_request(
        scope,  # type: ignore[arg-type]
        cast(Receive, receive),
        target_port,
        override_path=override_path,
        extra_headers=append_headers or None,
        timeout=timeout,
    )
    return _asgi_to_quart(proxied)


async def ws_proxy_quart(
    target_port: int,
    quart_ws: QuartWebsocket,
    identity_headers: dict[str, str] | None = None,
    override_path: str | None = None,
) -> None:
    """Quart-flavored wrapper around ``ws_proxy``.

    Wraps the underlying ASGI scope/receive/send in a Litestar ``WebSocket``
    so the new helper (which speaks Litestar's WS API) can drive it.  Quart's
    Websocket holds the receive/send callables on private attributes; their
    names match the ASGI app the websocket was constructed from.
    """
    scope = cast(Scope, quart_ws.scope)
    quart_ws_any = cast(Any, quart_ws)
    receive = cast(Receive, quart_ws_any._receive)
    send = quart_ws_any._send
    ws = WebSocket[Any, Any, Any](scope, receive, send)
    await ws_proxy(target_port, ws, identity_headers=identity_headers, override_path=override_path)
