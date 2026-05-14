import hashlib
from datetime import UTC
from datetime import datetime
from typing import Any

from quart import Request
from quart.wrappers import Websocket

from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.cookies import COOKIE_ACCESS


def _validate_api_token(token: str) -> dict[str, str] | None:
    """Validate a bearer token against the api_tokens table.

    Returns a claims dict (owner-level access) or None.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute(
        "SELECT name, expires_at FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
        return None
    owner = db.execute("SELECT username FROM owner WHERE id = 1").fetchone()
    if not owner:
        return None
    # TODO: give this a proper type?
    return {"sub": owner["username"], "username": owner["username"]}


def resolve_app_from_token(token: str) -> str | None:
    """Look up a Bearer token in the app_tokens table, return the app_id or None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT app_id FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    return row["app_id"] if row else None


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
        return _validate_api_token(auth_header.removeprefix("Bearer "))

    return None
