import sqlite3
from typing import Any

from litestar import Request
from litestar import Response
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.auth import validate_app_token
from compute_space.core.auth.auth import validate_session_token
from compute_space.db import get_db

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _get_bearer_token_if_set(connection: _AnyConnection) -> str | None:
    if auth_header := connection.headers.get("Authorization", ""):
        if auth_header.startswith("Bearer "):
            if token := auth_header.removeprefix("Bearer ").strip():
                return token
    return None


def authenticate(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    """Resolve who is making this request, by trying each auth scheme in priority order."""

    # TODO: attach origin information also. or maybe just make a helper and do this in routes?

    # session token in cookie
    if session_token := connection.cookies.get(SESSION_COOKIE_NAME):
        if authenticated_user := validate_session_token(session_token, db):
            return authenticated_user

    # api and app tokens are both set in Authorization: Bearer header
    if token := _get_bearer_token_if_set(connection):
        # api token
        if authenticated_api_token := validate_api_token(token, db):
            return authenticated_api_token

        # app token
        if authenticated_app := validate_app_token(token, db):
            return authenticated_app

    return None


class AuthMiddleware:
    """Validates and adds auth information to requests, on `request.accessor`.

    Auth isn't enforced here; missing auth will just yield `request.accessor = None`.
    Auth is actually enforced in route guards.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # passthrough types we don't handle (like what?)
        if scope["type"] not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            await self.app(scope, receive, send)
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)
        state = scope.setdefault("state", {})

        accessor = authenticate(request, get_db())
        state["accessor"] = accessor

        await self.app(scope, receive, send)


def login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /setup or /login; JSON clients get 401."""
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    db = get_db()
    user = db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if user is None:
        claim = request.query_params.get("claim", "")
        target = f"/setup?claim={claim}" if claim else "/setup"
    else:
        target = "/login"
    return Redirect(path=target)
