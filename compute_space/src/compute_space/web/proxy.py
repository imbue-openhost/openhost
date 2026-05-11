import asyncio
from typing import Any

import httpx
import websockets
from quart import Response
from quart.wrappers import Request
from quart.wrappers import Websocket
from werkzeug.datastructures import Headers

from compute_space.core.auth import COOKIE_REFRESH
from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown


async def proxy_request(
    quart_request: Request,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
) -> Response:
    """Forward a request to a local port.

    If override_path is set, use it instead of the request path.
    If extra_headers is set, merge them into the forwarded headers.
    """
    if override_path is not None:
        path = override_path
    else:
        # Use the raw (percent-encoded) path from the ASGI scope so that
        # URL-encoded characters (e.g. %3A, %40) are preserved exactly as
        # the client sent them.  This is critical for protocols like Matrix
        # federation where the sending server signs the original encoded URI.
        raw_path = quart_request.scope.get("raw_path")
        if raw_path is not None:
            path = raw_path.decode("ascii")
        else:
            path = quart_request.path
        if not path.startswith("/"):
            path = "/" + path

    target_url = f"http://127.0.0.1:{target_port}{path}"
    if quart_request.query_string:
        target_url += f"?{quart_request.query_string.decode('utf-8')}"

    excluded_headers = {
        "host",
        "connection",
        "transfer-encoding",
        "accept-encoding",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "cookie",
    }
    headers = {key: value for key, value in quart_request.headers if key.lower() not in excluded_headers}
    headers["X-Forwarded-For"] = quart_request.remote_addr or ""
    headers["X-Forwarded-Proto"] = quart_request.scheme
    headers["X-Forwarded-Host"] = quart_request.host

    if extra_headers:
        for k, v in extra_headers.items():
            if v is None:
                headers.pop(k, None)
            else:
                headers[k] = v

    try:
        body = await quart_request.get_data()
        cookies = {k: v for k, v in quart_request.cookies.items() if k not in (COOKIE_REFRESH,)}
        # Use granular timeouts: the default 30s is for connect/pool,
        # but read/write get a longer window to handle large uploads
        # (e.g. migration data transfers can be hundreds of MB).
        if isinstance(timeout, (int, float)):
            request_timeout = httpx.Timeout(
                connect=min(timeout, 30),
                read=max(timeout, 300),
                write=max(timeout, 300),
                pool=min(timeout, 30),
            )
        else:
            request_timeout = timeout
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=quart_request.method,
                url=target_url,
                headers=headers,
                content=body,
                cookies=cookies,
                follow_redirects=False,
                timeout=request_timeout,
            )
    except httpx.ConnectError:
        return Response("App is not responding", status=502)
    except httpx.TimeoutException:
        return Response("App timed out", status=504)
    except httpx.TransportError:
        return Response("App disconnected unexpectedly", status=502)

    response_excluded = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    }
    response_headers = Headers()
    for key, value in resp.headers.multi_items():
        if key.lower() not in response_excluded:
            response_headers.add(key, value)

    return Response(
        resp.content,
        status=resp.status_code,
        headers=response_headers,
    )


async def ws_proxy(target_port: int, client_ws: Websocket, identity_headers: dict[str, str] | None = None) -> None:
    """Bidirectionally proxy a WebSocket connection to a backend app.

    Uses Quart's native websocket object and the async websockets library.
    """
    # Prefer raw (percent-encoded) path to preserve URL encoding
    raw_path = client_ws.scope.get("raw_path")
    if raw_path is not None:
        path = raw_path.decode("ascii")
    else:
        path = client_ws.path
    if not path.startswith("/"):
        path = "/" + path

    target_url = f"ws://127.0.0.1:{target_port}{path}"
    query_string = client_ws.query_string.decode() if client_ws.query_string else ""
    if query_string:
        target_url += f"?{query_string}"

    # Forward relevant headers to the backend
    extra_headers = {}
    subprotocols = []
    for key, value in client_ws.headers:
        lower = key.lower()
        if lower == "sec-websocket-protocol":
            subprotocols = [s.strip() for s in value.split(",")]
        elif lower == "cookie":
            # Strip auth cookies, forward the rest
            filtered = "; ".join(
                part.strip() for part in value.split(";") if not part.strip().startswith((f"{COOKIE_REFRESH}=",))
            )
            if filtered:
                extra_headers[key] = filtered
        elif lower not in {
            "host",
            "connection",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "x-forwarded-for",
            "x-forwarded-proto",
            "x-forwarded-host",
        }:
            extra_headers[key] = value
    extra_headers["X-Forwarded-For"] = client_ws.remote_addr or ""
    extra_headers["X-Forwarded-Proto"] = client_ws.scheme
    extra_headers["X-Forwarded-Host"] = client_ws.host
    if identity_headers:
        extra_headers.update(identity_headers)

    # Accept the client WebSocket before connecting to the backend so
    # the handshake completes and both send/receive are immediately usable.
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
    # Compression is left at the websockets default (permessage-deflate offered): the previous
    # `compression=None` was overly cautious — RFC 7692 already mandates graceful fallback if the
    # backend rejects the extension, and enabling it dramatically reduces transfer sizes for the
    # text-heavy traffic this proxy commonly carries.
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
                            await client_ws.send(msg)
                        else:
                            await client_ws.send(msg)
                except Exception:
                    pass

            async def client_to_backend() -> None:
                try:
                    while True:
                        msg = await client_ws.receive()
                        if isinstance(msg, bytes):
                            await backend.send(msg)
                        else:
                            await backend.send(msg)
                except Exception:
                    pass

            # Run both directions concurrently. Also race against the global
            # shutdown event so that long-lived websocket sessions don't hold
            # hypercorn's graceful_timeout open during a restart — without this
            # the proxy will sit on each open connection until either side
            # closes naturally, dragging out shutdown by minutes.
            tasks = [
                asyncio.ensure_future(backend_to_client()),
                asyncio.ensure_future(client_to_backend()),
                asyncio.ensure_future(wait_for_shutdown()),
            ]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
    except Exception:
        logger.warning("WebSocket backend connection failed: %s", target_url)
