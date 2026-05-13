import asyncio
from typing import Any

import attr
import httpx
import websockets
from litestar import Response
from litestar import WebSocket
from litestar.types import Receive
from litestar.types import Scope

from compute_space.core.auth import COOKIE_REFRESH
from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown


@attr.s(auto_attribs=True, frozen=True)
class ProxiedResponse:
    """Bytes-in-memory snapshot of a proxied HTTP response, framework-neutral."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes
    media_type: str | None


def _scope_headers(scope: Scope) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in scope.get("headers", []):
        out.append((key.decode("latin-1"), value.decode("latin-1")))
    return out


def _scope_host(scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"host":
            return value.decode("latin-1")
    server = scope.get("server")
    if server:
        return f"{server[0]}:{server[1]}"
    return ""


def _scope_cookies(scope: Scope) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            for part in value.decode("latin-1").split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, val = part.partition("=")
                    cookies[name.strip()] = val.strip()
    return cookies


def _scope_remote_addr(scope: Scope) -> str:
    client = scope.get("client")
    if client:
        return str(client[0])
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


async def _do_proxy(
    scope: Scope,
    receive: Receive,
    target_port: int,
    override_path: str | None,
    extra_headers: dict[str, str | None] | None,
    timeout: float,
    body: bytes | None,
) -> ProxiedResponse:
    if override_path is not None:
        path = override_path
    else:
        raw_path = scope.get("raw_path")
        if raw_path is not None:
            path = raw_path.decode("ascii")
        else:
            path = scope.get("path", "/")
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
        "cookie",
    }
    headers: dict[str, str] = {}
    for key, value in _scope_headers(scope):
        if key.lower() not in excluded_headers:
            headers[key] = value
    headers["X-Forwarded-For"] = _scope_remote_addr(scope)
    headers["X-Forwarded-Proto"] = scope.get("scheme", "http")
    headers["X-Forwarded-Host"] = _scope_host(scope)

    if extra_headers:
        for k, v in extra_headers.items():
            if v is None:
                headers.pop(k, None)
            else:
                headers[k] = v

    if body is None:
        body = await _read_body(receive)
    cookies = {k: v for k, v in _scope_cookies(scope).items() if k not in (COOKIE_REFRESH,)}

    try:
        if isinstance(timeout, (int, float)):
            request_timeout: httpx.Timeout | float = httpx.Timeout(
                connect=min(timeout, 30),
                read=max(timeout, 300),
                write=max(timeout, 300),
                pool=min(timeout, 30),
            )
        else:
            request_timeout = timeout
        method: str = str(scope.get("method", "GET"))
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
                cookies=cookies,
                follow_redirects=False,
                timeout=request_timeout,
            )
    except httpx.ConnectError:
        return ProxiedResponse(status_code=502, headers=[], body=b"App is not responding", media_type="text/plain")
    except httpx.TimeoutException:
        return ProxiedResponse(status_code=504, headers=[], body=b"App timed out", media_type="text/plain")
    except httpx.TransportError:
        return ProxiedResponse(
            status_code=502, headers=[], body=b"App disconnected unexpectedly", media_type="text/plain"
        )

    response_excluded = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    }
    response_headers: list[tuple[str, str]] = []
    media_type: str | None = None
    for key, value in resp.headers.multi_items():
        lower = key.lower()
        if lower in response_excluded:
            continue
        if lower == "content-type":
            media_type = value
            continue
        response_headers.append((key, value))

    return ProxiedResponse(
        status_code=resp.status_code,
        headers=response_headers,
        body=resp.content,
        media_type=media_type,
    )


def proxied_to_litestar(proxied: ProxiedResponse) -> Response[bytes]:
    """Wrap a ProxiedResponse as a Litestar Response."""
    headers: dict[str, str] = {}
    for k, v in proxied.headers:
        # Litestar dict headers can't represent multi-value cookie sets cleanly; the proxy never returns set-cookie.
        headers[k] = v
    return Response(
        content=proxied.body,
        status_code=proxied.status_code,
        headers=headers,
        media_type=proxied.media_type,
    )


async def proxy_request(
    scope: Scope,
    receive: Receive,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
    body: bytes | None = None,
) -> Response[bytes]:
    """Forward an ASGI HTTP request to a local port and return a Litestar Response."""
    proxied = await _do_proxy(scope, receive, target_port, override_path, extra_headers, timeout, body)
    return proxied_to_litestar(proxied)


async def proxy_request_raw(
    scope: Scope,
    receive: Receive,
    target_port: int,
    override_path: str | None = None,
    extra_headers: dict[str, str | None] | None = None,
    timeout: float = 30,
    body: bytes | None = None,
) -> ProxiedResponse:
    """Same as ``proxy_request`` but returns the raw bytes/headers, useful for ASGI-level senders."""
    return await _do_proxy(scope, receive, target_port, override_path, extra_headers, timeout, body)


async def ws_proxy(
    target_port: int,
    client_ws: WebSocket[Any, Any, Any],
    identity_headers: dict[str, str] | None = None,
    override_path: str | None = None,
) -> None:
    """Bidirectionally proxy a WebSocket connection to a backend app."""
    if override_path is not None:
        path = override_path
    else:
        raw_path = client_ws.scope.get("raw_path")
        if raw_path is not None:
            path = raw_path.decode("ascii")
        else:
            path = client_ws.scope.get("path", "/")
    if not path.startswith("/"):
        path = "/" + path

    target_url = f"ws://127.0.0.1:{target_port}{path}"
    qs = client_ws.scope.get("query_string", b"")
    query_string = qs.decode() if qs else ""
    if query_string:
        target_url += f"?{query_string}"

    extra_headers: dict[str, str] = {}
    subprotocols: list[str] = []
    for key, value in _scope_headers(client_ws.scope):
        lower = key.lower()
        if lower == "sec-websocket-protocol":
            subprotocols = [s.strip() for s in value.split(",")]
        elif lower == "cookie":
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
    extra_headers["X-Forwarded-For"] = _scope_remote_addr(client_ws.scope)
    extra_headers["X-Forwarded-Proto"] = client_ws.scope.get("scheme", "ws")
    extra_headers["X-Forwarded-Host"] = _scope_host(client_ws.scope)
    if identity_headers:
        extra_headers.update(identity_headers)

    await client_ws.accept()

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
                        msg: Any = await client_ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        bytes_payload = msg.get("bytes")
                        text_payload = msg.get("text")
                        if bytes_payload is not None:
                            await backend.send(bytes_payload)
                        elif text_payload is not None:
                            await backend.send(text_payload)
                except Exception:
                    pass

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
