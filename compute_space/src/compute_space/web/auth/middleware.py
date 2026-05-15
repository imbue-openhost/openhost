import hashlib
import inspect
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from functools import wraps
from typing import Any
from urllib.parse import urlparse

from quart import Request
from quart import g
from quart import jsonify
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue
from quart.wrappers import Websocket

from compute_space.config import get_config
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import resolve_app_from_token
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.auth.tokens import decode_access_token_allow_expired
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import clear_auth_cookies


def get_current_user_from_request(request: Request | Websocket) -> dict[str, Any] | None:
    """Extract and verify identity from request cookies or Authorization header.

    Accepts either an HTTP Request or a Websocket — both expose .headers and .cookies.
    Checks JWT cookie first, then falls back to Authorization: Bearer token.
    Returns claims dict or None.

    TODO: return something with proper typing!
    """
    # Warn on duplicate auth cookies — this happens when cookies were set with
    # different Domain attributes (e.g. after a config change). The browser
    # sends both, but only the first is read, which may be stale/invalid.
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

    # Fall back to Authorization: Bearer (API tokens)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return validate_api_token(auth_header.removeprefix("Bearer "))

    return None


def _wants_json() -> bool:
    """Check if the client requested JSON via Accept header."""
    return "application/json" in request.headers.get("Accept", "")


def _app_action_response(app_id: str) -> ResponseReturnValue:
    """Return JSON for fetch/API calls, redirect for regular form submits."""
    if _wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("apps.app_detail", app_id=app_id))


async def _ensure_async(f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call f and await it if it's a coroutine, otherwise return directly."""
    result = f(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _try_refresh() -> dict[str, Any] | None:
    """Attempt to refresh an expired access token using the refresh cookie.
    Stores new access token on flask.g for after_request."""
    refresh_tok = request.cookies.get(COOKIE_REFRESH)
    if not refresh_tok:
        return None

    expired_jwt = request.cookies.get(COOKIE_ACCESS)
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

    # Refreshed access tokens must carry the *current* owner.username,
    # not the stale value captured in the now-expired token.  If the
    # operator changed their username via /api/settings/owner_username
    # while their session was alive, the next refresh would otherwise
    # ship a JWT whose ``sub`` no longer matches the persisted name —
    # and the proxy's owner-identity check (which compares ``sub`` to
    # ``owner.username``) would silently start dropping the
    # ``X-OpenHost-Is-Owner`` header for the rest of the session.
    #
    # Refusing to refresh when the owner row has gone away (post-wipe
    # / pre-setup) matches ``_validate_api_token``'s gate against the
    # same condition: a stale refresh-token cookie from before the
    # wipe shouldn't be enough to mint a fresh JWT against the new
    # zone.
    current_username = read_owner_username(db)
    if current_username is None:
        return None
    new_access_token = create_access_token(current_username)
    g.new_access_token = new_access_token
    g.refresh_token = refresh_tok
    return decode_access_token(new_access_token)


def _app_from_origin(req_or_ws: Request | Websocket) -> str | None:
    """Resolve app_id from Origin/Referer subdomain + valid JWT cookie.

    Accepts either a quart Request or Websocket — both expose .headers and are accepted
    by get_current_user_from_request.
    """
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
    """Identify+authenticate which app is making the request.

    Cross-app requests can come from two contexts: server-side (app-to-app via Bearer token) or client-side
    (browser JS calling the service proxy on behalf of an app). Both need to resolve to an app_id so the
    router can look up permissions and pass the caller's identity to the provider.

    Bearer token:   The app authenticates directly with its issued token.
    Browser cookie: The user is logged in (JWT) and the request originates from an app subdomain — the app_id
                    is derived from the Origin header (looked up by subdomain → name → app_id).

    Injects `app_id` as a keyword argument to the wrapped function.
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            app_id = resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
        else:
            app_id = _app_from_origin(request)

        if not app_id:
            return jsonify({"error": "Missing or invalid authorization"}), 401

        kwargs["app_id"] = app_id
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated


def login_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        claims = get_current_user_from_request(request)
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        # Try transparent refresh
        claims = _try_refresh()
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        # Not authenticated — redirect to setup or login.
        # If stale auth cookies are present (e.g. after key rotation on reboot),
        # clear them so they don't conflict with freshly issued cookies after login.
        has_stale_cookies = request.cookies.get(COOKIE_ACCESS) is not None

        db = get_db()
        owner = db.execute("SELECT * FROM owner").fetchone()
        if owner is None:
            # Preserve claim token in redirect so /setup can validate it
            claim = request.args.get("claim", "")
            response = redirect(url_for("setup.setup", claim=claim) if claim else url_for("setup.setup"))
        else:
            response = redirect(url_for("auth.login"))

        if has_stale_cookies:
            clear_auth_cookies(response, request=request)  # type: ignore[arg-type]
        return response

    return decorated
