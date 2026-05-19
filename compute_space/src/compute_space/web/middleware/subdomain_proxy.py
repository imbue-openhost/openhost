from typing import Any

from litestar import Request
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.apps import get_app_from_hostname
from compute_space.core.apps import is_public_path
from compute_space.web.auth.auth import verify_owner_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request

IS_OWNER_HEADER = ("X-OpenHost-Is-Owner", "true")


class SubdomainProxyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # note: we don't need to handle CORS here because cross-origin requests are not allowed (those go thru services which handles its own CORS).
        connection: ASGIConnection[Any, Any, Any, Any] = ASGIConnection(scope, receive, send)

        app = get_app_from_hostname(connection.url.netloc)
        if not app:
            # Not an app subdomain, pass through to router.
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
        except NotAuthorizedException as e:
            if not is_public_path(app, scope["path"]):
                raise e

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
