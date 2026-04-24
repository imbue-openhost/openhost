import hashlib
import inspect
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from functools import wraps
from typing import Any

from quart import g
from quart import jsonify
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.core import auth
from compute_space.core.auth import resolve_app_from_token
from compute_space.db import get_db


def _wants_json() -> bool:
    """Check if the client requested JSON via Accept header."""
    return "application/json" in request.headers.get("Accept", "")


def _app_action_response(app_name: str) -> ResponseReturnValue:
    """Return JSON for fetch/API calls, redirect for regular form submits."""
    if _wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("apps.app_detail", app_name=app_name))


async def _ensure_async(f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call f and await it if it's a coroutine, otherwise return directly."""
    result = f(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _try_refresh() -> dict[str, Any] | None:
    """Attempt to refresh an expired access token using the refresh cookie.
    Stores new access token on flask.g for after_request."""
    refresh_tok = request.cookies.get(auth.COOKIE_REFRESH)
    if not refresh_tok:
        return None

    expired_jwt = request.cookies.get(auth.COOKIE_ACCESS)
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
    g.new_access_token = new_access_token
    g.refresh_token = refresh_tok
    return auth.decode_access_token(new_access_token)


def app_token_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid app token"}), 401
        app_name = resolve_app_from_token(auth_header.removeprefix("Bearer "))
        if not app_name:
            return jsonify({"error": "Missing or invalid app token"}), 401
        kwargs["app_name"] = app_name
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated


def login_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        claims = auth.get_current_user_from_request(request)
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        # Try transparent refresh
        claims = _try_refresh()
        if claims is not None:
            return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

        # Not authenticated — redirect to setup or login.
        # If stale auth cookies are present (e.g. after key rotation on reboot),
        # clear them so they don't conflict with freshly issued cookies after login.
        has_stale_cookies = request.cookies.get(auth.COOKIE_ACCESS) is not None

        db = get_db()
        owner = db.execute("SELECT * FROM owner").fetchone()
        if owner is None:
            # Preserve claim token in redirect so /setup can validate it
            claim = request.args.get("claim", "")
            response = redirect(url_for("auth.setup", claim=claim) if claim else url_for("auth.setup"))
        else:
            response = redirect(url_for("auth.login"))

        if has_stale_cookies:
            auth.clear_auth_cookies(response, request=request)  # type: ignore[arg-type]
        return response

    return decorated
