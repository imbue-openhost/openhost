"""ASGI middleware: when the access JWT was missing/expired but a refresh cookie carried us through
auth, mint a new access JWT and attach Set-Cookie headers to the response.
"""

from typing import Any
from typing import cast

from litestar import Request
from litestar.enums import ScopeType
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.auth.jwt_tokens import create_access_token
from compute_space.core.auth.jwt_tokens import decode_access_token
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import build_auth_cookies


class AuthRefreshMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != ScopeType.HTTP:
            await self.app(scope, receive, send)
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start" and (cookies := _refresh_cookies(request, scope)):
                headers_obj: Any = message.get("headers", [])
                headers = list(headers_obj) if headers_obj else []
                for cookie in cookies:
                    headers.append(cookie.to_encoded_header())
                cast(Any, message)["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


def _refresh_cookies(request: Request[Any, Any, Any], scope: Scope) -> list[Any]:
    """Return cookies to attach if this request authenticated via the refresh path, else []."""
    accessor = (scope.get("state") or {}).get("accessor")
    if not isinstance(accessor, AuthenticatedUser):
        return []
    refresh_tok = request.cookies.get(COOKIE_REFRESH)
    if not refresh_tok:
        return []
    access_tok = request.cookies.get(COOKIE_ACCESS)
    if access_tok and decode_access_token(access_tok) is not None:
        return []  # JWT was already valid; not a refresh request
    new_access = create_access_token(accessor.username)
    return build_auth_cookies(new_access, refresh_tok, request_host=request.headers.get("host", ""))
