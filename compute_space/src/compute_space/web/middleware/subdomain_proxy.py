from typing import Any

from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types.asgi_types import WebSocketCloseEvent

from compute_space.config import get_config
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.apps import is_public_path
from compute_space.web.auth.auth import login_required_redirect
from compute_space.web.auth.auth import verify_owner_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request

IS_OWNER_HEADER = ("X-OpenHost-Is-Owner", "true")


def _looks_like_app_subdomain(netloc: str) -> bool:
    """True iff ``netloc`` looks like ``<something>.<zone_domain>`` (i.e. the
    request hit what looks like an app subdomain of the configured zone).
    The router itself answers on ``zone_domain`` exactly, not on a subdomain.
    """
    host = netloc.split(":", 1)[0]
    zone = get_config().zone_domain
    return bool(zone) and host.endswith("." + zone)


class SubdomainProxyMiddleware:
    """Outer ASGI middleware: intercepts requests on app subdomains and forwards
    them to the app's local container port.  Runs *before* Litestar so app-subdomain
    requests never go through Litestar's routing — which means we don't have a
    resolved route_handler in scope and Litestar's per-handler abstractions
    don't apply.  Non-app requests (router subdomain, etc.) pass through to
    the wrapped Litestar app unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            # lifespan etc. — pass through.
            await self.app(scope, receive, send)
            return

        # note: we don't need to handle CORS here because cross-origin requests are not allowed (those go thru services which handles its own CORS).
        connection: ASGIConnection[Any, Any, Any, Any] = ASGIConnection(scope, receive, send)

        app = get_app_from_hostname(connection.url.netloc)
        if not app:
            if _looks_like_app_subdomain(connection.url.netloc):
                # The hostname looks like an app subdomain but no app is deployed
                # there — return 404 instead of falling through to the router
                if scope["type"] == ScopeType.HTTP:
                    request: Request[Any, Any, Any] = Request(scope, receive, send)
                    response: Response[Any] = Response(content=None, status_code=404)
                    await response.to_asgi_response(app=None, request=request)(scope, receive, send)
                else:
                    await send(WebSocketCloseEvent(type="websocket.close", code=4404, reason="no such app"))
                return
            # Router subdomain (or unrelated host) — defer to Litestar.
            await self.app(scope, receive, send)
            return

        # TODO: maybe behave differently for apps that are not in running state. not sure

        # add forwarding headers for the openhost app, so it can tell where the request came from.
        # these are annoying but unavoidable - we can't spoof the IP or proto in the forwarded request.
        # X-Forwarded-Host preserves the original Host so apps that build absolute URLs don't use the proxy's internal hostname
        extra_headers = [("X-Forwarded-Host", connection.url.netloc)]
        if connection.client:
            # client IP; for some reason this is allowed to be None in ASGI. port should not be included.
            extra_headers.append(("X-Forwarded-For", f"{connection.client.host}"))

        try:
            verify_owner_auth(connection)
            extra_headers.append(IS_OWNER_HEADER)
        except NotAuthorizedException:
            if not is_public_path(app, scope["path"]):
                # We're outer ASGI middleware — a raised NotAuthorizedException
                # wouldn't reach Litestar's exception handlers, so produce the
                # equivalent response ourselves.  HTTP: same /login redirect the
                # exception handler would emit, dispatched via Litestar's
                # Redirect→ASGI machinery.  WS: refuse the handshake.
                if scope["type"] == ScopeType.HTTP:
                    request = Request(scope, receive, send)
                    response = login_required_redirect(request)
                    await response.to_asgi_response(app=None, request=request)(scope, receive, send)
                else:
                    await send(
                        WebSocketCloseEvent(type="websocket.close", code=4401, reason="authentication required")
                    )
                return

        if scope["type"] == ScopeType.HTTP:
            extra_headers.append(("X-Forwarded-Proto", scope["scheme"]))
            proxied = await proxy_http_request(
                Request(scope, receive, send),
                target_port=app.local_port,
                extra_headers=extra_headers,
            )
            await proxied(scope, receive, send)
        else:
            assert scope["type"] == ScopeType.WEBSOCKET
            await proxy_websocket_request(
                WebSocket(scope, receive, send),
                target_port=app.local_port,
                extra_headers=extra_headers,
            )
