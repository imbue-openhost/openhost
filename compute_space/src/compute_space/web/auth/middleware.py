"""Litestar dependencies and exception handlers for auth.

``provide_user`` resolves the current user (with transparent refresh) and ``provide_app_id`` resolves the
calling app's id from a Bearer token or Origin subdomain. ``login_required_redirect`` is the catch-all
exception handler for unauthenticated requests.
"""

import hashlib
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
from compute_space.core import auth
from compute_space.core.auth import resolve_app_from_token
from compute_space.db import get_db
from compute_space.web.auth.cookies import cleared_auth_cookies
from compute_space.web.auth.inputs import auth_inputs_from_connection

# ASGIConnection is parametrized; alias for clarity in dependency signatures.
_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _try_refresh(connection: _AnyConnection) -> dict[str, Any] | None:
    """Attempt to refresh an expired access token using the refresh cookie.

    On success, stashes the new access/refresh tokens in ``scope['state']`` so
    ``AuthRefreshMiddleware`` can attach them to the response.
    """
    refresh_tok = connection.cookies.get(auth.COOKIE_REFRESH)
    if not refresh_tok:
        return None

    expired_jwt = connection.cookies.get(auth.COOKIE_ACCESS)
    if not expired_jwt:
        return None

    expired_claims = auth.decode_access_token_allow_expired(expired_jwt)
    if expired_claims is None:
        return None

    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    db = get_db()
    rt = db.execute(
        "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None:
        return None

    expires_at = datetime.fromisoformat(rt["expires_at"])
    if expires_at < datetime.now(UTC):
        return None

    new_access_token = auth.create_access_token(expired_claims["sub"])
    state = connection.scope.setdefault("state", {})
    state["new_access_token"] = new_access_token
    state["refresh_token"] = refresh_tok
    return auth.decode_access_token(new_access_token)


def _app_from_origin(connection: _AnyConnection) -> str | None:
    """Resolve app_id from Origin/Referer subdomain + valid JWT cookie."""
    if not auth.get_current_user(auth_inputs_from_connection(connection)):
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

    row = get_db().execute("SELECT app_id FROM apps WHERE name = ?", (app_name,)).fetchone()
    return row["app_id"] if row else None


async def provide_user(request: Request[Any, Any, Any]) -> dict[str, Any]:
    """Litestar dependency that returns the current user's claims, refreshing tokens if needed."""
    claims = auth.get_current_user(auth_inputs_from_connection(request))
    if claims is not None:
        return claims
    refreshed = _try_refresh(request)
    if refreshed is not None:
        return refreshed
    raise NotAuthorizedException(detail="Authentication required")


async def provide_app_id(request: Request[Any, Any, Any]) -> str:
    """Litestar dependency that resolves the calling app's id (Bearer token or Origin subdomain)."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        app_id = resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
    else:
        app_id = _app_from_origin(request)

    if not app_id:
        raise NotAuthorizedException(detail="Missing or invalid authorization")
    return app_id


def _wants_json(request: Request[Any, Any, Any]) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /setup or /login; JSON clients get 401."""
    if _wants_json(request):
        return Response(
            content={"error": "Missing or invalid authorization"},
            status_code=401,
        )

    has_stale_cookies = request.cookies.get(auth.COOKIE_ACCESS) is not None
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        claim = request.query_params.get("claim", "")
        target = f"/setup?claim={claim}" if claim else "/setup"
        response: Response[Any] = Redirect(path=target)
    else:
        response = Redirect(path="/login")

    if has_stale_cookies:
        request_host = request.headers.get("host", "")
        for cookie in cleared_auth_cookies(request_host):
            response.set_cookie(cookie)
    return response
