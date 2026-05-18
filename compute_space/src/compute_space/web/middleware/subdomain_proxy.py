import asyncio
from collections.abc import AsyncIterator
from collections.abc import Container
from collections.abc import Iterable
from typing import Any
from typing import cast

import httpx
import websockets
from litestar import Request
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.response.base import ASGIResponse
from litestar.response.streaming import ASGIStreamingResponse
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.apps import get_app_from_hostname
from compute_space.core.apps import is_public_path
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown
from compute_space.web.auth.auth import AppOrigin
from compute_space.web.auth.auth import RouterOrigin
from compute_space.web.auth.auth import get_accessor
from compute_space.web.auth.auth import get_origin

IS_OWNER_HEADER = ("X-OpenHost-Is-Owner", "true")

# auth cookies must never reach a backend app
_STRIPPED_COOKIES = frozenset({SESSION_COOKIE_NAME})

# The router is the sole authority for X-OpenHost-* identity headers.
# Any inbound value would let a client spoof identity to the backend app.
_OPENHOST_HEADER_PREFIX = "x-openhost-"


def _verify_owner(scope: Scope, target_app_id: str) -> bool:
    # TODO: redo this
    """is this request treated as "owner"-origin for subdomain proxy auth purposes?

    YES:
    - users authed via cookie (ie browser) with router or same-app origin. cross-app origin is rejected, as it could be forged by untrusted app js.
    - API keys with any origin
    NO:
    - cross-app origin
    - app tokens
    """
    accessor = get_accessor(scope)
    origin = get_origin(scope)

    if isinstance(accessor, AuthenticatedUser):
        if isinstance(origin, RouterOrigin):
            return True
        if isinstance(origin, AppOrigin) and origin.app_id == target_app_id:
            return True
    if isinstance(accessor, AuthenticatedAPIKey):
        return True
    return False


def _get_request_target_hostname(scope: Scope) -> str:
    """Host header with a fallback to ``scope['server']`` for synthesised scopes."""
    host = Headers.from_scope(scope).get("host")
    if host:
        return host
    server = scope.get("server")
    if server:
        return f"{server[0]}:{server[1]}"
    return ""


class SubdomainProxyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        connection: ASGIConnection[Any, Any, Any, Any] = ASGIConnection(scope, receive, send)

        host = _get_request_target_hostname(scope)
        app = get_app_from_hostname(host)
        if not app:
            # Not an app subdomain, pass through to router.
            await self.app(scope, receive, send)
            return

        # TODO: maybe behave differently for apps that are not in running state. not sure

        # add forwarding headers for the openhost app, so it can tell where the request came from.
        # these are annoying but unavoidable - we can't spoof the IP or proto in the forwarded request.
        # i don't think x-forwarded-host is needed?
        extra_headers = []
        if connection.client:
            # client IP; for some reason this is allowed to be None in ASGI
            extra_headers.append(("X-Forwarded-For", f"{connection.client.host}:{connection.client.port}"))

        is_owner = _verify_owner(scope, target_app_id=app.app_id)
        if is_owner:
            extra_headers.append(IS_OWNER_HEADER)
        else:
            if not is_public_path(app, scope["path"]):
                raise NotAuthorizedException(detail="Authentication required to access this path")

        if scope["type"] == ScopeType.HTTP:
            extra_headers.append(("X-Forwarded-Proto", scope["scheme"]))
            proxied = await proxy_request(
                Request(scope, receive, send),
                target_port=app.local_port,
                extra_headers=extra_headers,
            )
            await proxied(scope, receive, send)
        else:
            assert scope["type"] == ScopeType.WEBSOCKET
            await ws_proxy(WebSocket(scope, receive, send), target_port=app.local_port, extra_headers=extra_headers)
            await send(cast(Message, {"type": "websocket.close", "code": 1008}))


def _sanitize_forwarded_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Filter inbound headers before forwarding to a backend app.

    Drops X-OpenHost-* headers (the router is their sole authority) and strips
    zone auth cookies from the Cookie header (apps must not see or replay the
    owner's session).  Protocol-level filtering (Host, Connection, etc.) is
    left to each caller.
    """
    cookie_prefixes = tuple(f"{name}=" for name in _STRIPPED_COOKIES)
    sanitized: list[tuple[str, str]] = []
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
        sanitized.append((key, value))
    return sanitized


def _format_proxy_request_url(scope: Scope, target_port: int, override_path: str | None = None) -> str:
    if override_path:
        path = override_path
    else:
        # Use the raw (percent-encoded) path from the ASGI scope so that
        # URL-encoded characters (e.g. %3A, %40) are preserved exactly as
        # the client sent them.  This is critical for protocols like Matrix
        # federation where the sending server signs the original encoded URI.
        path = scope["raw_path"].decode("ascii")

    path = path.lstrip("/")
    target_url = f"http://127.0.0.1:{target_port}/{path}"
    query_string = scope["query_string"]
    if query_string:
        target_url += f"?{query_string.decode('utf-8')}"
    return target_url


# Request hop-by-hop headers + the X-Forwarded-* triplet that we (re)set
# ourselves below — never copied from the client.
_REQUEST_EXCLUDED_HEADERS = frozenset(
    {
        "host",
        "connection",
        "transfer-encoding",
        "accept-encoding",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
)

# Response framing headers that have to be regenerated by the outer ASGI
# layer based on the body we actually emit, not blindly forwarded.
_RESPONSE_EXCLUDED_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    }
)


def _build_forwarded_request_headers(
    request: Request[Any, Any, Any],
    extra_headers: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    headers = _sanitize_forwarded_headers(
        (k, v) for k, v in request.headers.items() if k.lower() not in _REQUEST_EXCLUDED_HEADERS
    )
    headers.extend(extra_headers)
    return headers


def _add_response_headers(response: ASGIResponse, items: Iterable[tuple[str, str]]) -> None:
    """Append headers to an ASGIResponse via its MutableScopeHeaders storage.

    ``ASGIStreamingResponse.headers`` only accepts a dict at construction time,
    which collapses duplicates (Set-Cookie, Link, …).  Mutating after the fact
    via ``.add()`` is the only way to forward duplicates faithfully.
    """
    for k, v in items:
        response.headers.add(k, v)


def _split_forwarded_response_headers(
    items: Iterable[tuple[str, str]],
) -> tuple[list[tuple[str, str]], str | None]:
    """Strip framing headers and lift content-type so ASGIResponse can own both.

    Returned as a list of tuples (not a dict) because providers may legitimately
    emit duplicates (Set-Cookie, Link, …) and dict-shaped headers would
    collapse them.
    """
    out: list[tuple[str, str]] = []
    media_type: str | None = None
    for key, value in items:
        lower = key.lower()
        if lower in _RESPONSE_EXCLUDED_HEADERS:
            continue
        if lower == "content-type" and media_type is None:
            media_type = value
            continue
        out.append((key, value))
    return out, media_type


async def proxy_request(
    request: Request[Any, Any, Any],
    target_port: int,
    override_path: str | None = None,
    extra_headers: Iterable[tuple[str, str]] = (),
    timeout: float = 30,
    buffer_status_codes: Container[int] = frozenset({403}),
) -> ASGIResponse:
    """Forward an HTTP request to a local port and return the response as an
    ASGIResponse (streaming by default).

    The request body is streamed straight from the client into httpx via
    ``request.stream()``; the response is streamed back via
    ``ASGIStreamingResponse`` so neither direction has to fit in memory.

    When the upstream status is in ``buffer_status_codes`` (default: just 403)
    the body is buffered into bytes instead and a plain ``ASGIResponse`` is
    returned.  This is the hook services_v2 uses to inspect 403 responses and
    inject ``grant_url`` before relaying them.
    """
    target_url = _format_proxy_request_url(request.scope, target_port, override_path)
    new_headers = _build_forwarded_request_headers(request, extra_headers)

    client = httpx.AsyncClient(timeout=timeout)
    try:
        req = client.build_request(
            method=str(request.method),
            url=target_url,
            headers=new_headers,
            content=request.stream(),
        )
        try:
            upstream = await client.send(req, stream=True)
        except httpx.ConnectError:
            await client.aclose()
            return ASGIResponse(body=b"App is not responding", status_code=502, media_type="text/plain")
        except httpx.TimeoutException:
            await client.aclose()
            return ASGIResponse(body=b"App timed out", status_code=504, media_type="text/plain")
        except httpx.TransportError:
            await client.aclose()
            return ASGIResponse(body=b"App disconnected unexpectedly", status_code=502, media_type="text/plain")

        response_headers, media_type = _split_forwarded_response_headers(upstream.headers.multi_items())

        if upstream.status_code in buffer_status_codes:
            body = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            buffered = ASGIResponse(
                body=body,
                status_code=upstream.status_code,
                media_type=media_type,
            )
            _add_response_headers(buffered, response_headers)
            return buffered

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        streaming = ASGIStreamingResponse(
            iterator=stream_body(),
            status_code=upstream.status_code,
            media_type=media_type,
        )
        _add_response_headers(streaming, response_headers)
        return streaming
    except BaseException:
        # If we never reached the return-with-iterator path we have to close
        # the client ourselves; otherwise the cleanup happens inside
        # stream_body's finally.
        await client.aclose()
        raise


async def ws_proxy(
    connection: WebSocket[Any, Any, Any],
    target_port: int,
    extra_headers: Iterable[tuple[str, str]] = (),
    override_path: str | None = None,
) -> None:
    """Bidirectionally proxy a WebSocket connection to a backend app.

    Uses Litestar's WebSocket and the async ``websockets`` library.  If
    ``override_path`` is set, use it instead of the client path.
    """
    target_url = _format_proxy_request_url(connection.scope, target_port, override_path)

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
    for key, value in connection.headers.items():
        if key.lower() == "sec-websocket-protocol":
            subprotocols = [s.strip() for s in value.split(",")]
            continue
        if key.lower() in excluded_headers:
            continue
        forwardable.append((key, value))

    new_headers = _sanitize_forwarded_headers(forwardable)
    new_headers.extend(extra_headers)

    # Accept the client WebSocket before connecting to the backend so the
    # handshake completes and both send/receive are immediately usable.
    await connection.accept()

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
                            await connection.send_bytes(msg)
                        else:
                            await connection.send_text(msg)
                except Exception:
                    pass

            async def client_to_backend() -> None:
                try:
                    while True:
                        msg = await connection.receive()
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
