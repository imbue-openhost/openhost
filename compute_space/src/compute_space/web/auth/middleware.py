import hashlib
import inspect
import sqlite3
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from functools import wraps
from typing import Any
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect
from quart import Request as QuartRequest
from quart import g
from quart import jsonify
from quart import redirect as quart_redirect
from quart import request as quart_request
from quart import url_for
from quart.typing import ResponseReturnValue
from quart.wrappers import Websocket as QuartWebsocket

from compute_space.config import get_config
from compute_space.core import auth
from compute_space.core.auth import resolve_app_from_token
from compute_space.core.auth import validate_api_token
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.auth.tokens import decode_access_token_allow_expired
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import clear_auth_cookies
from compute_space.web.auth.inputs import auth_inputs_from_connection

# ─── Quart-flavored helpers (for unmigrated route files) ───


def get_current_user_from_request(request: QuartRequest | QuartWebsocket) -> dict[str, Any] | None:
    """Extract and verify identity from a Quart request/websocket cookies or Auth header.

    Used by the unmigrated Quart route files.  New Litestar handlers go through
    ``auth.get_current_user(auth_inputs_from_connection(...))`` instead.
    """
    cookie_header = request.headers.get("Cookie", "")
    dupes = cookie_header.count(f"{COOKIE_ACCESS}=")
    if dupes > 1:
        logger.warning(
            "Duplicate %s cookies detected (%d instances) for %s %s. "
            "This usually means cookies were set with different Domain attributes. "
            "The user should clear cookies to fix this.",
            COOKIE_ACCESS,
            dupes,
            getattr(request, "method", "WS"),
            request.path,
        )

    token = request.cookies.get(COOKIE_ACCESS)
    if token:
        claims = decode_access_token(token)
        if claims:
            return claims

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return validate_api_token(auth_header.removeprefix("Bearer "))

    return None


def _wants_json() -> bool:
    return "application/json" in quart_request.headers.get("Accept", "")


def _app_action_response(app_id: str) -> ResponseReturnValue:
    """Return JSON for fetch/API calls, redirect for regular form submits."""
    if _wants_json():
        return jsonify({"ok": True})
    return quart_redirect(url_for("apps.app_detail", app_id=app_id))


async def _ensure_async(f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call f and await it if it's a coroutine, otherwise return directly."""
    result = f(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _try_refresh() -> dict[str, Any] | None:
    """Attempt to refresh an expired access token using the refresh cookie.
    Stores new access token on flask.g for after_request."""
    refresh_tok = quart_request.cookies.get(COOKIE_REFRESH)
    if not refresh_tok:
        return None

    expired_jwt = quart_request.cookies.get(COOKIE_ACCESS)
    if not expired_jwt:
        return None

    expired_claims = decode_access_token_allow_expired(expired_jwt)
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

    new_access_token = create_access_token(expired_claims["sub"])
    g.new_access_token = new_access_token
    g.refresh_token = refresh_tok
    return decode_access_token(new_access_token)


def _app_from_origin(req_or_ws: QuartRequest | QuartWebsocket) -> str | None:
    """Resolve app_id from Origin/Referer subdomain + valid JWT cookie."""
    if not get_current_user_from_request(req_or_ws):
        return None

    origin = req_or_ws.headers.get("Origin", "") or req_or_ws.headers.get("Referer", "")
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


def app_auth_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    """Quart decorator: identify+authenticate which app is making the request.

    Bearer token -> the app authenticates directly.
    Browser cookie -> user is logged in and request originates from app subdomain.
    Injects ``app_id`` as a kwarg.  Used only by unmigrated Quart routes.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        auth_header = quart_request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            app_id = resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
        else:
            app_id = _app_from_origin(quart_request)

        if not app_id:
            return jsonify({"error": "Missing or invalid authorization"}), 401

        kwargs["app_id"] = app_id
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated


def login_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    """Quart decorator that redirects unauthenticated requests to /setup or /login.

    Used only by unmigrated Quart routes.  Litestar handlers depend on
    ``provide_user`` and let ``login_required_redirect`` handle the redirect.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        claims = get_current_user_from_request(quart_request)
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        claims = _try_refresh()
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        has_stale_cookies = quart_request.cookies.get(COOKIE_ACCESS) is not None

        db = get_db()
        owner = db.execute("SELECT * FROM owner").fetchone()
        if owner is None:
            claim = quart_request.args.get("claim", "")
            response = quart_redirect(url_for("auth.setup", claim=claim) if claim else url_for("auth.setup"))
        else:
            response = quart_redirect(url_for("auth.login"))

        if has_stale_cookies:
            clear_auth_cookies(response, request=quart_request)  # type: ignore[arg-type]
        return response

    return decorated


# ─── Litestar dependencies (for migrated route files) ───


_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _try_refresh_for_connection(connection: _AnyConnection, db: sqlite3.Connection) -> dict[str, Any] | None:
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

    row = db.execute("SELECT app_id FROM apps WHERE name = ?", (app_name,)).fetchone()
    return row["app_id"] if row else None


async def provide_user(request: Request[Any, Any, Any], db: sqlite3.Connection) -> dict[str, Any]:
    """Litestar dependency that returns the current user's claims, refreshing tokens if needed."""
    claims = auth.get_current_user(auth_inputs_from_connection(request))
    if claims is not None:
        return claims
    refreshed = _try_refresh_for_connection(request, db)
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
