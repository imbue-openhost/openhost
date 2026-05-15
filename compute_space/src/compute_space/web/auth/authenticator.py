import hashlib
import sqlite3
from datetime import UTC
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from litestar.connection import ASGIConnection

from compute_space.config import get_config
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.auth.jwt_tokens import create_access_token
from compute_space.core.auth.jwt_tokens import decode_access_token
from compute_space.core.auth.jwt_tokens import decode_access_token_allow_expired
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def authenticate(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    """Resolve who is making this request, by trying each auth scheme in priority order.

    On a successful refresh-token rotation, the new access token is stashed in
    ``scope["state"]`` so ``AuthRefreshMiddleware`` can attach a Set-Cookie header
    to the outgoing response.
    """
    user = _try_jwt_cookie(connection)
    if user is not None:
        return user

    refreshed = _try_refresh(connection, db)
    if refreshed is not None:
        return refreshed

    bearer = _try_bearer(connection, db)
    if bearer is not None:
        return bearer

    return _try_origin_subdomain(connection, db)


def _try_jwt_cookie(connection: _AnyConnection) -> AuthenticatedUser | None:
    token = connection.cookies.get(COOKIE_ACCESS)
    if not token:
        return None
    claims = decode_access_token(token)
    if claims is None:
        return None
    return AuthenticatedUser(username=claims["sub"])


def _try_refresh(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedUser | None:
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
    if datetime.fromisoformat(rt["expires_at"]) < datetime.now(UTC):
        return None

    username = expired_claims["sub"]
    new_access_token = create_access_token(username)
    state = connection.scope.setdefault("state", {})
    state["new_access_token"] = new_access_token
    state["refresh_token"] = refresh_tok
    return AuthenticatedUser(username=username)


def _try_bearer(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    auth_header = connection.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    api_row = db.execute(
        "SELECT expires_at FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if api_row is not None:
        expires_at = api_row["expires_at"]
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now(UTC):
            return None
        return AuthenticatedAPIKey()

    app_row = db.execute(
        "SELECT app_id FROM app_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if app_row is not None:
        return AuthenticatedApp(app_id=app_row["app_id"])

    return None


def _try_origin_subdomain(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedApp | None:
    """Validate that an Origin/Referer subdomain is one of our apps, gated on a valid JWT cookie.

    The JWT cookie must validate (a logged-in user is calling from inside an app's iframe/page); we
    then trust the Origin to identify which app they're acting on behalf of.
    """
    if _try_jwt_cookie(connection) is None:
        return None

    origin = connection.headers.get("Origin", "") or connection.headers.get("Referer", "")
    if not origin:
        return None

    parsed = urlparse(origin)
    host = parsed.netloc or ""
    zone = get_config().zone_domain
    if not zone or not host.endswith("." + zone):
        return None

    app_name = host[: -(len(zone) + 1)]
    if "." in app_name:
        return None

    row = db.execute("SELECT app_id FROM apps WHERE name = ?", (app_name,)).fetchone()
    if row is None:
        return None
    return AuthenticatedApp(app_id=row["app_id"])
