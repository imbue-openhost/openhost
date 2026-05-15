from typing import Any

from litestar import Request
from litestar.enums import ScopeType
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.db import get_db
from compute_space.web.auth.authenticator import authenticate


class AuthAccessorMiddleware:
    """Authenticates each request and stashes the AuthenticatedAccessor (or None) in scope state.

    Never raises -- guards on individual route handlers are responsible for enforcing that the
    accessor is present and of the right type.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            await self.app(scope, receive, send)
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)
        accessor = authenticate(request, get_db())
        state = scope.setdefault("state", {})
        state["accessor"] = accessor
        await self.app(scope, receive, send)
