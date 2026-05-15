"""ASGI middleware that proxies app-subdomain requests directly to backend ports.

If the host doesn't parse as an app subdomain, or the owner hasn't been verified yet, the request is passed through to
the regular Litestar router.
"""

from typing import Any
from typing import cast

from litestar import Request
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.config import get_config
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import is_public_path
from compute_space.core.apps import parse_app_from_host
from compute_space.db import close_db
from compute_space.db import get_db
from compute_space.web.auth.middleware import get_current_user
from compute_space.web.auth.middleware import try_refresh_tokens
from compute_space.web.proxy import ProxiedResponse
from compute_space.web.proxy import _scope_host
from compute_space.web.proxy import proxy_request
from compute_space.web.proxy import ws_proxy


def _identity_headers(claims: dict[str, str] | None) -> dict[str, str]:
    if claims and claims.get("sub") == "owner":
        return {"X-OpenHost-Is-Owner": "true"}
    return {}


def _content_length(scope: Scope) -> int:
    for key, value in scope.get("headers", []):
        if key.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _is_websocket_upgrade(scope: Scope) -> bool:
    for key, value in scope.get("headers", []):
        if key.lower() == b"upgrade" and value.lower() == b"websocket":
            return True
    return False


def _owner_verified(scope: Scope) -> bool:
    app = scope.get("app")
    if app is None:
        return False
    state = getattr(app, "state", None)
    if state is None:
        return False
    return bool(getattr(state, "owner_verified", False))


async def _send_proxied(send: Send, proxied: ProxiedResponse) -> None:
    headers: list[tuple[bytes, bytes]] = []
    if proxied.media_type:
        headers.append((b"content-type", proxied.media_type.encode("latin-1")))
    for k, v in proxied.headers:
        headers.append((k.encode("latin-1"), v.encode("latin-1")))
    await send(cast(Message, {"type": "http.response.start", "status": proxied.status_code, "headers": headers}))
    await send(cast(Message, {"type": "http.response.body", "body": proxied.body}))


async def _send_simple(send: Send, status: int, body: bytes) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    await send(cast(Message, {"type": "http.response.start", "status": status, "headers": headers}))
    await send(cast(Message, {"type": "http.response.body", "body": body}))


async def _send_redirect(send: Send, location: str) -> None:
    headers = [(b"location", location.encode("latin-1"))]
    await send(cast(Message, {"type": "http.response.start", "status": 302, "headers": headers}))
    await send(cast(Message, {"type": "http.response.body", "body": b""}))


def _scope_proto(scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"x-forwarded-proto":
            return value.decode("latin-1")
    return scope.get("scheme", "http")


class SubdomainProxyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            await self.app(scope, receive, send)
            return

        if not _owner_verified(scope):
            await self.app(scope, receive, send)
            return

        host = _scope_host(scope)
        app_subdomain = parse_app_from_host(host)
        if not app_subdomain:
            await self.app(scope, receive, send)
            return

        # The short-circuit branches below opens a connection via ``get_db()`` (contextvar-backed)
        # for the auth lookup but bypasses the Litestar router, so the routed-path ``after_request``
        # hook that normally closes per-request connections never fires.  Close it here.
        try:
            if scope_type == ScopeType.HTTP:
                await self._handle_http(scope, receive, send, app_subdomain)
            else:
                await self._handle_websocket(scope, receive, send, app_subdomain)
        finally:
            close_db()

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send, app_subdomain: str) -> None:
        if _is_websocket_upgrade(scope):
            # Hypercorn negotiates WS upgrade through the websocket scope, but
            # an HTTP request with Upgrade: websocket can occasionally arrive;
            # let it fall through to the router.
            await self.app(scope, receive, send)
            return

        app_row = find_app_by_name(app_subdomain)
        if not app_row:
            await _send_simple(send, 404, f"App '{app_subdomain}' not found".encode())
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)
        claims = get_current_user(request)
        if claims is None:
            claims = try_refresh_tokens(request, get_db())

        path = scope.get("path", "/")
        if claims is None and not is_public_path(app_row, path):
            redirect_url = f"{_scope_proto(scope)}://{get_config().zone_domain}/login"
            await _send_redirect(send, redirect_url)
            return

        # Use a longer timeout for large requests (e.g. migration data transfers).
        timeout = 600 if _content_length(scope) > 10 * 1024 * 1024 else 30
        proxied = await proxy_request(
            scope,
            receive,
            app_row["local_port"],
            extra_headers=cast(dict[str, str | None], _identity_headers(claims)),
            timeout=timeout,
        )
        await _send_proxied(send, proxied)

    async def _handle_websocket(self, scope: Scope, receive: Receive, send: Send, app_subdomain: str) -> None:
        app_row = find_app_by_name(app_subdomain)
        if app_row and app_row["status"] in ("running", "starting"):
            ws = WebSocket[Any, Any, Any](scope, receive, send)
            connection: ASGIConnection[Any, Any, Any, Any] = ws
            claims = get_current_user(connection)
            path = scope.get("path", "/")
            if claims is not None or is_public_path(app_row, path):
                await ws_proxy(app_row["local_port"], ws, identity_headers=_identity_headers(claims))
                return
        await send(cast(Message, {"type": "websocket.close", "code": 1008}))
