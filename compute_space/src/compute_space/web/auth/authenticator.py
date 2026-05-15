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
    return (
        _try_jwt_cookie(connection)
        or _try_refresh(connection, db)
        or _try_bearer(connection, db)
        or _try_origin_subdomain(connection, db)
    )


def _try_jwt_cookie(connection: _AnyConnection) -> AuthenticatedUser | None:
    if not (token := connection.cookies.get(COOKIE_ACCESS)):
        return None
    if (claims := decode_access_token(token)) is None:
        return None
    return AuthenticatedUser(username=claims["sub"])


def _try_refresh(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedUser | None:
    """Authenticate by validating the refresh-token cookie + the (allowed-expired) JWT cookie.

    Pure check: no side effects. ``AuthRefreshMiddleware`` separately decides whether to mint a
    fresh access cookie based on the same conditions.
    """
    if not (refresh_tok := connection.cookies.get(COOKIE_REFRESH)):
        return None
    if not (expired_jwt := connection.cookies.get(COOKIE_ACCESS)):
        return None
    if (expired_claims := decode_access_token_allow_expired(expired_jwt)) is None:
        return None

    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    rt = db.execute(
        "SELECT expires_at FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None or datetime.fromisoformat(rt["expires_at"]) < datetime.now(UTC):
        return None

    return AuthenticatedUser(username=expired_claims["sub"])


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
