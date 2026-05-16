import sqlite3
from typing import Any
from urllib.parse import urlparse

import attr
from litestar import Request
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.config import get_config
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.auth import validate_app_token
from compute_space.core.auth.auth import validate_session_token
from compute_space.db import get_db

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


@attr.s(auto_attribs=True, frozen=True)
class RequestOrigin:
    """Location where the request claims to originate from."""

    origin: str | None


@attr.s(auto_attribs=True, frozen=True)
class RouterOrigin(RequestOrigin):
    """Request claims to originate from the router itself."""

    pass


@attr.s(auto_attribs=True, frozen=True)
class AppOrigin(RequestOrigin):
    """Request claims to originate from an app subdomain."""

    app_id: str


def get_accessor(scope: Scope) -> AuthenticatedAccessor | None:
    state = scope.get("state") or {}
    return state.get("accessor")


def get_origin(scope: Scope) -> RequestOrigin | None:
    state = scope.get("state") or {}
    return state.get("origin")


def _get_bearer_token_if_set(connection: _AnyConnection) -> str | None:
    if auth_header := connection.headers.get("Authorization", ""):
        if auth_header.startswith("Bearer "):
            if token := auth_header.removeprefix("Bearer ").strip():
                return token
    return None


def _get_connection_origin(connection: _AnyConnection) -> str | None:
    """yields the request's stated origin, including port if specified and non-default.

    ie "sub.example.com" or "sub.example.com:1234", no protocol or path.
    returns None if no origin can be determined.
    """
    raw = connection.headers.get("Origin") or connection.headers.get("Referer")
    if not raw:
        return None
    parsed = urlparse(raw)
    host, port = parsed.hostname, parsed.port
    if not host:
        return None
    # Drop default ports so :443/:80 match the bare host form
    if (parsed.scheme, port) in {("https", 443), ("http", 80)}:
        port = None
    return f"{host}:{port}" if port else host


def authenticate(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    """Resolve who is making this request, by trying each auth scheme in priority order."""

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
    """Validates and adds auth information to requests, on `request.accessor` and `request.origin`.

    Origin is not (cannot be) validated, but is useful for route guards to make auth decisions based on where the request claims to come from.

    Auth isn't enforced here; missing auth will just yield `request.accessor = None`; it should be enforced in route guards.
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

        origin_str = _get_connection_origin(request)
        if origin_str == get_config().zone_domain:
            origin = RouterOrigin(origin=origin_str)
        elif app := get_app_from_hostname(origin_str):
            origin = AppOrigin(origin=origin_str, app_id=app.app_id)
        else:
            origin = RequestOrigin(origin=origin_str)
        state["origin"] = origin

        await self.app(scope, receive, send)
