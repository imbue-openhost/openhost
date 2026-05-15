"""ASGI middleware that proxies app-subdomain requests directly to backend ports.

If the host doesn't parse as an app subdomain, or the owner hasn't been verified yet, the request is passed through to
the regular Litestar router.
"""

from typing import Any
from typing import cast

from litestar import Request
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.response.base import ASGIResponse
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.config import get_config
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import is_public_path
from compute_space.core.apps import parse_app_from_host
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.db import close_db
from compute_space.db import get_db
from compute_space.web.auth.authenticator import authenticate
from compute_space.web.proxy import _scope_host
from compute_space.web.proxy import proxy_request
from compute_space.web.proxy import ws_proxy


def _identity_headers(accessor: AuthenticatedAccessor | None) -> dict[str, str]:
    if isinstance(accessor, AuthenticatedAPIKey):
        return {"X-OpenHost-Is-Owner": "true"}
    if isinstance(accessor, AuthenticatedUser) and accessor.username == "owner":
        return {"X-OpenHost-Is-Owner": "true"}
    return {}


def _owner_verified(scope: Scope) -> bool:
    app = scope.get("app")
    if app is None:
        return False
    state = getattr(app, "state", None)
    if state is None:
        return False
    return bool(getattr(state, "owner_verified", False))


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
        headers = Headers.from_scope(scope)
        if headers.get("upgrade", "").lower() == "websocket":
            # Hypercorn negotiates WS upgrade through the websocket scope, but
            # an HTTP request with Upgrade: websocket can occasionally arrive;
            # let it fall through to the router.
            await self.app(scope, receive, send)
            return

        app_row = find_app_by_name(app_subdomain)
        if not app_row:
            body = f"App '{app_subdomain}' not found".encode()
            await ASGIResponse(body=body, status_code=404, media_type="text/plain")(scope, receive, send)
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)
        accessor = authenticate(request, get_db())

        path = scope.get("path", "/")
        if accessor is None and not is_public_path(app_row, path):
            proto = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
            redirect_url = f"{proto}://{get_config().zone_domain}/login"
            await ASGIResponse(status_code=302, headers=[("location", redirect_url)], media_type="text/plain")(
                scope, receive, send
            )
            return

        # Use a longer timeout for large requests (e.g. migration data transfers).
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        timeout = 600 if content_length > 10 * 1024 * 1024 else 30
        proxied = await proxy_request(
            scope,
            receive,
            app_row["local_port"],
            extra_headers=cast(dict[str, str | None], _identity_headers(accessor)),
            timeout=timeout,
        )
        await proxied(scope, receive, send)

    async def _handle_websocket(self, scope: Scope, receive: Receive, send: Send, app_subdomain: str) -> None:
        app_row = find_app_by_name(app_subdomain)
        if app_row and app_row["status"] in ("running", "starting"):
            ws = WebSocket[Any, Any, Any](scope, receive, send)
            connection: ASGIConnection[Any, Any, Any, Any] = ws
            accessor = authenticate(connection, get_db())
            path = scope.get("path", "/")
            if accessor is not None or is_public_path(app_row, path):
                await ws_proxy(app_row["local_port"], ws, identity_headers=_identity_headers(accessor))
                return
        await send(cast(Message, {"type": "websocket.close", "code": 1008}))
