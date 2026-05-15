"""ASGI middleware that attaches refreshed-auth cookies to outgoing HTTP responses.

Auth dependencies place the new tokens in ``scope["state"]`` (under
``new_access_token`` / ``refresh_token``); this middleware appends the
corresponding ``Set-Cookie`` headers to the ``http.response.start`` message so
downstream handlers never have to mention cookies.
"""

from typing import Any
from typing import cast

from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.web.auth.cookies import build_auth_cookies


class AuthRefreshMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != ScopeType.HTTP:
            await self.app(scope, receive, send)
            return

        host = Headers.from_scope(scope).get("host", "")

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state = scope.get("state") or {}
                new_access = state.get("new_access_token")
                if new_access:
                    refresh = state.get("refresh_token")
                    headers_obj: Any = message.get("headers", [])
                    headers = list(headers_obj) if headers_obj else []
                    for cookie in build_auth_cookies(new_access, refresh, request_host=host):
                        headers.append(cookie.to_encoded_header())
                    cast(Any, message)["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)
