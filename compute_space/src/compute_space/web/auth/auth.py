# note: we don't need to handle CORS in the main auth path because cross-origin requests are not allowed.
# the only allowed cross-origin requests go thru the services interface which handles its own CORS.
import sqlite3
from typing import Any
from urllib.parse import urlparse

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler

from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.auth import validate_app_token
from compute_space.core.auth.auth import validate_session_token
from compute_space.db import get_db

AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _get_bearer_token_if_set(connection: AnyConnection) -> str | None:
    if auth_header := connection.headers.get("Authorization", ""):
        if auth_header.startswith("Bearer "):
            if token := auth_header.removeprefix("Bearer ").strip():
                return token
    return None


def get_connection_origin(connection: AnyConnection) -> str | None:
    """gets and formats the origin header as "sub.example.com" or "sub.example.com:1234", no protocol or path, if set.
    port is included if non-default.
    returns None if no origin header is set.

    browsers have specific behaviors around the Origin header, which we rely on here.
    - it is set on all cross-origin requests, including from subdomains.
    - it is not set on all same-origin requests though
    - it includes the port if non-default (not 80 or 443).

    we don't use the Referer header, as it's not intended for use in CORS type origin validation.
    """
    if raw := connection.headers.get("Origin"):
        parsed = urlparse(raw)
        host, port = parsed.hostname, parsed.port
        if not host:
            return None
        return f"{host}:{port}" if port else host
    return None


def authenticate(connection: AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
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


def verify_owner_auth(connection: AnyConnection) -> None:
    """Verify that the request is authenticated as an "owner" (either a user or an API key, with valid Origin).

    returns if authed; raises NotAuthorizedException if not authenticated.
    """
    accessor = authenticate(connection, db=get_db())
    origin = get_connection_origin(connection)

    if isinstance(accessor, AuthenticatedUser):
        if origin is not None and origin != connection.base_url.netloc:
            # if origin is set (it is set on all browser cross-origin requests and cannot be forged by js),
            # it must match the target URL. either router-origin or same-app-origin is fine.
            # we never allow cross-origin requests with user auth, even from other subdomains, as these could be forged by untrusted app js.
            raise NotAuthorizedException(detail="user authentication only valid for router-origin requests")
        # origin is not set on normal same-origin GETs, for example, so we allow these.
        return
    if isinstance(accessor, AuthenticatedAPIKey):
        # API key requests won't come from untrusted JS, so can be trusted regardless of origin.
        return
    raise NotAuthorizedException(detail="User or API key authentication required")


def verify_app_auth(connection: AnyConnection) -> str:
    """Verify that the request is authenticated as an "app" (either client-side, from app js, or server-side, from an app token).

    returns `app_id` if authed; raises NotAuthorizedException if not authenticated.
    """
    accessor = authenticate(connection, db=get_db())
    origin = get_connection_origin(connection)

    if isinstance(accessor, AuthenticatedUser):
        if origin is not None and (app := get_app_from_hostname(origin)) is not None:
            # requests from app js come from the user's browser with the user's auth.
            # Origin will always be set by the browser on these cross-origin requests.
            return app.app_id
    if isinstance(accessor, AuthenticatedApp):
        # server-side app requests.
        return accessor.app_id
    raise NotAuthorizedException(detail="app authentication required")


def require_owner_auth(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_owner_auth to be used as a route guard."""
    verify_owner_auth(connection)


def require_app_auth(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_app_auth to be used as a route guard."""
    verify_app_auth(connection)
