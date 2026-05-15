import hashlib
import sqlite3
from datetime import UTC
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect

from compute_space.config import get_config
from compute_space.core.auth.auth import resolve_app_from_token
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.auth.tokens import decode_access_token_allow_expired
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def get_current_user(connection: _AnyConnection) -> dict[str, Any] | None:
    """Extract and verify identity from a Litestar connection's cookies or Authorization header.

    Checks the JWT cookie first, then falls back to ``Authorization: Bearer`` API tokens.
    """
    cookie_header = connection.headers.get("Cookie", "")
    dupes = cookie_header.count(f"{COOKIE_ACCESS}=")
    if dupes > 1:
        logger.warning(
            "Duplicate %s cookies detected (%d instances) for %s %s. "
            "This usually means cookies were set with different Domain attributes. "
            "The user should clear cookies to fix this.",
            COOKIE_ACCESS,
            dupes,
            str(connection.scope.get("method") or "WS"),
            connection.scope.get("path", "/"),
        )

    token = connection.cookies.get(COOKIE_ACCESS)
    if token:
        claims = decode_access_token(token)
        if claims:
            return claims

    auth_header = connection.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return validate_api_token(auth_header.removeprefix("Bearer "))

    return None


def try_refresh_tokens(connection: _AnyConnection, db: sqlite3.Connection) -> dict[str, Any] | None:
    """Attempt to refresh an expired access token using the refresh cookie.

    On success, stashes the new access/refresh tokens in ``scope['state']`` so
    ``AuthRefreshMiddleware`` can attach them to the response.
    """
    refresh_tok = connection.cookies.get(COOKIE_REFRESH)
    if not refresh_tok:
        return None

    expired_jwt = connection.cookies.get(COOKIE_ACCESS)
    if not expired_jwt:
        return None

    expired_claims = decode_access_token_allow_expired(expired_jwt)
    if expired_claims is None:
        return None

    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    rt = db.execute(
        "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None:
        return None

    expires_at = datetime.fromisoformat(rt["expires_at"])
    if expires_at < datetime.now(UTC):
        return None

    new_access_token = create_access_token(expired_claims["sub"])
    state = connection.scope.setdefault("state", {})
    state["new_access_token"] = new_access_token
    state["refresh_token"] = refresh_tok
    return decode_access_token(new_access_token)


def _app_from_origin_for_connection(connection: _AnyConnection, db: sqlite3.Connection) -> str | None:
    """Resolve app_id from Origin/Referer subdomain + valid JWT cookie."""
    if not get_current_user(connection):
        return None

    origin = connection.headers.get("Origin", "") or connection.headers.get("Referer", "")
    if not origin:
        return None

    parsed = urlparse(origin)
    host = parsed.netloc or ""
    config = get_config()
    if not config.zone_domain or not host.endswith("." + config.zone_domain):
        return None

    app_name = host[: -(len(config.zone_domain) + 1)]
    if "." in app_name:
        return None

    row = db.execute("SELECT app_id FROM apps WHERE name = ?", (app_name,)).fetchone()
    return row["app_id"] if row else None


async def provide_user(request: Request[Any, Any, Any], db: sqlite3.Connection) -> dict[str, Any]:
    """Litestar dependency that returns the current user's claims, refreshing tokens if needed."""
    claims = get_current_user(request)
    if claims is not None:
        return claims
    refreshed = try_refresh_tokens(request, db)
    if refreshed is not None:
        return refreshed
    raise NotAuthorizedException(detail="Authentication required")


async def provide_app_id(request: Request[Any, Any, Any], db: sqlite3.Connection) -> str:
    """Litestar dependency that resolves the calling app's id (Bearer token or Origin subdomain)."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        app_id = resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
    else:
        app_id = _app_from_origin_for_connection(request, db)

    if not app_id:
        raise NotAuthorizedException(detail="Missing or invalid authorization")
    return app_id


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
