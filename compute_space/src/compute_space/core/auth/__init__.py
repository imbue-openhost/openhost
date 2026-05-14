"""Convenience re-exports for the most-used auth helpers."""

from typing import Any

from compute_space.core.auth.auth import resolve_app_from_token
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.cookies import COOKIE_ACCESS
from compute_space.core.auth.cookies import COOKIE_REFRESH
from compute_space.core.auth.cookies import CookieSpec
from compute_space.core.auth.cookies import build_auth_cookies
from compute_space.core.auth.cookies import cleared_auth_cookies
from compute_space.core.auth.cookies import cookie_domain
from compute_space.core.auth.inputs import AuthInputs
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.auth.tokens import create_refresh_token
from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.auth.tokens import decode_access_token_allow_expired
from compute_space.core.logging import logger

__all__ = [
    "AuthInputs",
    "COOKIE_ACCESS",
    "COOKIE_REFRESH",
    "CookieSpec",
    "build_auth_cookies",
    "cleared_auth_cookies",
    "cookie_domain",
    "create_access_token",
    "create_refresh_token",
    "decode_access_token",
    "decode_access_token_allow_expired",
    "get_current_user",
    "resolve_app_from_token",
    "validate_api_token",
]


def get_current_user(inputs: AuthInputs) -> dict[str, Any] | None:
    """Extract and verify identity from neutral auth inputs.

    Checks the JWT cookie first, then falls back to ``Authorization: Bearer`` API tokens.
    """
    # Warn on duplicate auth cookies — this happens when cookies were set with
    # different Domain attributes (e.g. after a config change).  The browser
    # sends both, but only the first is read, which may be stale/invalid.
    dupes = inputs.cookie_header.count(f"{COOKIE_ACCESS}=")
    if dupes > 1:
        logger.warning(
            "Duplicate %s cookies detected (%d instances) for %s %s. "
            "This usually means cookies were set with different Domain attributes. "
            "The user should clear cookies to fix this.",
            COOKIE_ACCESS,
            dupes,
            inputs.method,
            inputs.path,
        )

    token = inputs.cookies.get(COOKIE_ACCESS)
    if token:
        claims = decode_access_token(token)
        if claims:
            return claims

    if inputs.auth_header.startswith("Bearer "):
        return validate_api_token(inputs.auth_header.removeprefix("Bearer "))

    return None
