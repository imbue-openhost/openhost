import hashlib
import sqlite3
from datetime import UTC
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from quart import Response

from compute_space.config import get_config
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.auth.jwt_tokens import create_access_token
from compute_space.core.auth.jwt_tokens import validate_jwt_access_token
from compute_space.db import get_db
from compute_space.web.auth.authenticator import authenticate
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import build_auth_cookies

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def authenticate(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    """Resolve who is making this request, by trying each auth scheme in priority order.

    On a successful refresh-token rotation, the new access token is stashed in
    ``scope["state"]`` so ``AuthRefreshMiddleware`` can attach a Set-Cookie header
    to the outgoing response.
    """

    # JWT access token in cookie
    if access_token := connection.cookies.get(COOKIE_ACCESS):
        if authenticated_user := validate_jwt_access_token(access_token):
            return authenticated_user

    return None
    return (
        _try_jwt_cookie(connection)
        or _try_refresh(connection, db)
        or _try_bearer(connection, db)
        or _try_origin_subdomain(connection, db)
    )


def _try_jwt_cookie(connection: _AnyConnection) -> AuthenticatedUser | None:
    if not (token := connection.cookies.get(COOKIE_ACCESS)):
        return None
    if (claims := validate_jwt_access_token(token)) is None:
        return None
    return AuthenticatedUser(username=claims["sub"])


def _try_bearer(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    auth_header = connection.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    if not (token := auth_header.removeprefix("Bearer ").strip()):
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    if api_row := db.execute("SELECT expires_at FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone():
        expires_at = api_row["expires_at"]
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now(UTC):
            return None
        return AuthenticatedAPIKey()

    if app_row := db.execute("SELECT app_id FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone():
        return AuthenticatedApp(app_id=app_row["app_id"])

    return None


def _try_origin_subdomain(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedApp | None:
    """Validate that an Origin/Referer subdomain is one of our apps, gated on a valid JWT cookie.

    The JWT cookie must validate (a logged-in user is calling from inside an app's iframe/page); we
    then trust the Origin to identify which app they're acting on behalf of.
    """
    if _try_jwt_cookie(connection) is None:
        return None

    if not (origin := connection.headers.get("Origin", "") or connection.headers.get("Referer", "")):
        return None

    host = urlparse(origin).netloc
    zone = get_config().zone_domain
    if not zone or not host.endswith("." + zone):
        return None

    app_name = host[: -(len(zone) + 1)]
    if "." in app_name:
        return None

    row = db.execute("SELECT app_id FROM apps WHERE name = ?", (app_name,)).fetchone()
    return AuthenticatedApp(app_id=row["app_id"]) if row else None


def _refresh_cookies(request: Request[Any, Any, Any], scope: Scope) -> list[Any]:
    """Return cookies to attach if this request authenticated via the refresh path, else []."""
    accessor = (scope.get("state") or {}).get("accessor")
    if not isinstance(accessor, AuthenticatedUser):
        return []
    refresh_tok = request.cookies.get(COOKIE_REFRESH)
    if not refresh_tok:
        return []
    access_tok = request.cookies.get(COOKIE_ACCESS)
    if access_tok and validate_jwt_access_token(access_tok) is not None:
        return []  # JWT was already valid; not a refresh request
    new_access = create_access_token(accessor.username)
    return build_auth_cookies(new_access, refresh_tok, request_host=request.headers.get("host", ""))


class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # passthrough types we don't handle (like what?)
        if scope["type"] not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            await self.app(scope, receive, send)
            return

        request: Request[Any, Any, Any] = Request(scope, receive, send)
        state = scope.setdefault("state", {})

        # auth
        accessor = authenticate(request, get_db())
        state["accessor"] = accessor

        # send updated JWT cookie if expired, on outgoing response
        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                if cookies := _refresh_cookies(request, scope):
                    headers_obj: Any = message.get("headers", [])
                    headers = list(headers_obj) if headers_obj else []
                    for cookie in cookies:
                        headers.append(cookie.to_encoded_header())
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


async def provide_accessor(request: Request[Any, Any, Any]) -> AuthenticatedAccessor:
    """Litestar dependency: return the AuthenticatedAccessor populated by AuthAccessorMiddleware."""
    state = request.scope.get("state") or {}
    accessor = state.get("accessor")
    if accessor is None:
        raise NotAuthorizedException(detail="Authentication required")
    return accessor


def login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /setup or /login; JSON clients get 401."""
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        claim = request.query_params.get("claim", "")
        target = f"/setup?claim={claim}" if claim else "/setup"
    else:
        target = "/login"
    return Redirect(path=target)
