import asyncio
from collections.abc import Iterable
from typing import Any
from typing import cast

import httpx
import websockets
from litestar import WebSocket
from litestar.datastructures import Headers
from litestar.response.base import ASGIResponse
from litestar.types import Receive
from litestar.types import Scope
from quart import Response as QuartResponse
from quart.wrappers import Request as QuartRequest
from quart.wrappers import Websocket as QuartWebsocket
from werkzeug.datastructures import Headers as WerkzeugHeaders

from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH

# Zone auth cookies must never reach a backend app — apps share the zone
# domain, so forwarding these would let any app replay the owner's session
# against compute_space admin APIs or other apps.
_STRIPPED_COOKIES = frozenset({COOKIE_ACCESS, COOKIE_REFRESH})

# The router is the sole authority for X-OpenHost-* identity headers.
# Any inbound value would let a client spoof identity to the backend app.
_OPENHOST_HEADER_PREFIX = "x-openhost-"


def _sanitize_forwarded_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Filter inbound headers before forwarding to a backend app.

    Drops X-OpenHost-* headers (the router is their sole authority) and strips
    zone auth cookies from the Cookie header (apps must not see or replay the
    owner's session).  Protocol-level filtering (Host, Connection, etc.) is
    left to each caller.
    """
    cookie_prefixes = tuple(f"{name}=" for name in _STRIPPED_COOKIES)
    sanitized: dict[str, str] = {}
    for key, value in headers:
        lower = key.lower()
        if lower.startswith(_OPENHOST_HEADER_PREFIX):
            continue
        if lower == "cookie":
            value = "; ".join(
                part.strip() for part in value.split(";") if not part.strip().startswith(cookie_prefixes)
            )
            if not value:
                continue
        sanitized[key] = value
    return sanitized


def _scope_host(scope: Scope) -> str:
    """Host header with a fallback to ``scope['server']`` for synthesised scopes."""
    host = Headers.from_scope(scope).get("host")
    if host:
        return host
    server = scope.get("server")
    if server:
        return f"{server[0]}:{server[1]}"
    return ""


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _request_timeout(timeout: float) -> httpx.Timeout:
    """Granular timeouts: short connect/pool, long read/write for big uploads."""
    return httpx.Timeout(
        connect=min(timeout, 30),
        read=max(timeout, 300),
        write=max(timeout, 300),
        pool=min(timeout, 30),
    )


async def proxy_request(
    scope: Scope,
    receive: Receive,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
) -> ASGIResponse:
    """Forward an HTTP request to a local port.

    If ``override_path`` is set, use it instead of the request path.
    If ``extra_headers`` is set, merge them into the forwarded headers
    (a value of ``None`` deletes a header).
    """
    if override_path is not None:
        path = override_path
    else:
        # Use the raw (percent-encoded) path from the ASGI scope so that
        # URL-encoded characters (e.g. %3A, %40) are preserved exactly as
        # the client sent them.  This is critical for protocols like Matrix
        # federation where the sending server signs the original encoded URI.
        raw_path = scope.get("raw_path")
        path = raw_path.decode("ascii") if raw_path is not None else scope.get("path", "/")
        if not path.startswith("/"):
            path = "/" + path

    target_url = f"http://127.0.0.1:{target_port}{path}"
    qs = scope.get("query_string", b"")
    if qs:
        target_url += f"?{qs.decode('utf-8')}"

    excluded_headers = {
        "host",
        "connection",
        "transfer-encoding",
        "accept-encoding",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
    headers = _sanitize_forwarded_headers(
        (k, v) for k, v in Headers.from_scope(scope).items() if k.lower() not in excluded_headers
    )
    scope_client = scope.get("client")
    headers["X-Forwarded-For"] = str(scope_client[0]) if scope_client else ""
    headers["X-Forwarded-Proto"] = scope.get("scheme", "http")
    headers["X-Forwarded-Host"] = _scope_host(scope)

    if extra_headers:
        for k, v in extra_headers.items():
            if v is None:
                headers.pop(k, None)
            else:
                headers[k] = v

    body = await _read_body(receive)
    method = str(scope.get("method", "GET"))

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
                follow_redirects=False,
                timeout=_request_timeout(timeout),
            )
    except httpx.ConnectError:
        return ASGIResponse(body=b"App is not responding", status_code=502, media_type="text/plain")
    except httpx.TimeoutException:
        return ASGIResponse(body=b"App timed out", status_code=504, media_type="text/plain")
    except httpx.TransportError:
        return ASGIResponse(body=b"App disconnected unexpectedly", status_code=502, media_type="text/plain")

    response_excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers: list[tuple[str, str]] = []
    media_type: str | None = None
    for key, value in resp.headers.multi_items():
        lower = key.lower()
        if lower in response_excluded:
            continue
        if lower == "content-type" and media_type is None:
            media_type = value
            continue
        response_headers.append((key, value))

    return ASGIResponse(
        body=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=media_type,
    )


async def ws_proxy(
    target_port: int,
    client_ws: WebSocket[Any, Any, Any],
    identity_headers: dict[str, str] | None = None,
    override_path: str | None = None,
) -> None:
    """Bidirectionally proxy a WebSocket connection to a backend app.

    Uses Litestar's WebSocket and the async ``websockets`` library.  If
    ``override_path`` is set, use it instead of the client path.
    """
    scope = client_ws.scope
    if override_path is not None:
        path = override_path
    else:
        raw_path = scope.get("raw_path")
        path = raw_path.decode("ascii") if raw_path is not None else scope.get("path", "/")
    if not path.startswith("/"):
        path = "/" + path

    target_url = f"ws://127.0.0.1:{target_port}{path}"
    qs = scope.get("query_string", b"")
    if qs:
        target_url += f"?{qs.decode('utf-8')}"

    excluded_headers = {
        "host",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
    subprotocols: list[str] = []
    forwardable: list[tuple[str, str]] = []
    for key, value in Headers.from_scope(scope).items():
        lower = key.lower()
        if lower == "sec-websocket-protocol":
            subprotocols = [s.strip() for s in value.split(",")]
            continue
        if lower in excluded_headers:
            continue
        forwardable.append((key, value))
    extra_headers = _sanitize_forwarded_headers(forwardable)
    scope_client = scope.get("client")
    extra_headers["X-Forwarded-For"] = str(scope_client[0]) if scope_client else ""
    extra_headers["X-Forwarded-Proto"] = scope.get("scheme", "http")
    extra_headers["X-Forwarded-Host"] = _scope_host(scope)
    if identity_headers:
        extra_headers.update(identity_headers)

    # Accept the client WebSocket before connecting to the backend so the
    # handshake completes and both send/receive are immediately usable.
    await client_ws.accept()

    # Only pass `subprotocols` if the client actually negotiated some.  Passing an empty list causes the
    # `websockets` client to emit an empty `Sec-WebSocket-Protocol:` header, which strict backends
    # (including `websockets`' own server, as used by Selkies / the linuxserver webtop image) reject
    # with `InvalidHeaderFormat: expected token at 0 in`.
    #
    # `max_size=None` lifts the default 1 MiB incoming-message cap: the proxy shouldn't impose its
    # own size policy — apps decide their own limits, and a 1 MiB ceiling here silently kills any
    # backend that legitimately sends a larger frame (e.g. a CRDT's initial-state sync).
    #
    # Compression is left at the websockets default (permessage-deflate offered).
    ws_kwargs: dict[str, Any] = {
        "additional_headers": extra_headers,
        "max_size": None,
        "open_timeout": 10,
        "close_timeout": 5,
    }
    if subprotocols:
        ws_kwargs["subprotocols"] = subprotocols

    try:
        async with websockets.connect(target_url, **ws_kwargs) as backend:

            async def backend_to_client() -> None:
                try:
                    async for msg in backend:
                        if isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                        else:
                            await client_ws.send_text(msg)
                except Exception:
                    pass

            async def client_to_backend() -> None:
                try:
                    while True:
                        msg = await client_ws.receive()
                        msg_type = msg.get("type")
                        if msg_type == "websocket.disconnect":
                            return
                        if msg_type != "websocket.receive":
                            continue
                        if (raw_bytes := msg.get("bytes")) is not None:
                            await backend.send(cast(bytes, raw_bytes))
                        elif (raw_text := msg.get("text")) is not None:
                            await backend.send(cast(str, raw_text))
                except Exception:
                    pass

            # Race both directions against the global shutdown event so that
            # long-lived websocket sessions don't hold hypercorn's
            # graceful_timeout open during a restart — without this the proxy
            # would sit on each open connection until either side closes
            # naturally, dragging out shutdown by minutes.
            tasks = [
                asyncio.ensure_future(backend_to_client()),
                asyncio.ensure_future(client_to_backend()),
                asyncio.ensure_future(wait_for_shutdown()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
    except Exception:
        logger.warning("WebSocket backend connection failed: %s", target_url)


# ─── Quart-flavored adapters (used by unmigrated routes/services_v2.py) ───
#
# These wrap the ASGI-native helpers above so unmigrated Quart blueprints can
# keep their existing call shape.  They will be removed once all routes are
# migrated.


def _asgi_to_quart(proxied: ASGIResponse) -> QuartResponse:
    headers = WerkzeugHeaders()
    media_type: str | None = None
    for key_b, value_b in proxied.encoded_headers:
        key = key_b.decode("latin-1")
        lower = key.lower()
        # Quart computes its own content-length from the body.
        if lower == "content-length":
            continue
        # ASGIResponse synthesises a single content-type from media_type — peel it
        # back off and pass via Quart's content_type kwarg so it isn't double-set.
        if lower == "content-type":
            media_type = value_b.decode("latin-1")
            continue
        headers.add(key, value_b.decode("latin-1"))
    body = proxied.body if isinstance(proxied.body, bytes) else proxied.body.encode("utf-8")
    return QuartResponse(body, status=proxied.status_code, headers=headers, content_type=media_type)


async def proxy_request_quart(
    quart_request: QuartRequest,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
) -> QuartResponse:
    """Quart-flavored wrapper around ``proxy_request``.

    Buffers the body off the Quart request (which has already consumed the ASGI
    receive callable) and synthesizes a one-shot receive for the new helper.
    """
    body = await quart_request.get_data()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    proxied = await proxy_request(
        cast(Scope, quart_request.scope),
        cast(Receive, receive),
        target_port,
        override_path=override_path,
        extra_headers=extra_headers,
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

    Builds a Litestar ``WebSocket`` view of the same ASGI scope/receive/send
    callables and delegates.  Quart's ``Websocket`` doesn't expose ``_send``
    publicly so we go through the underlying ``_send_callable``.
    """
    scope = cast(Scope, quart_ws.scope)
    # Quart's Websocket holds the receive/send pair on private attributes; the names match the
    # ASGI app it was constructed from.
    quart_ws_any = cast(Any, quart_ws)
    receive = cast(Receive, quart_ws_any._receive)
    send = quart_ws_any._send
    ws = WebSocket[Any, Any, Any](scope, receive, send)
    await ws_proxy(target_port, ws, identity_headers=identity_headers, override_path=override_path)
