"""Quart-side cookie wrappers — translate framework-neutral CookieSpec to Response.set_cookie.

Used by the unmigrated Quart route files (setup, login, etc.).  Migrated
Litestar handlers should construct ``litestar.datastructures.Cookie`` from
``CookieSpec`` directly instead of going through these wrappers.
"""

from quart import Request
from quart import Response

from compute_space.core.auth.cookies import COOKIE_ACCESS as COOKIE_ACCESS
from compute_space.core.auth.cookies import COOKIE_REFRESH as COOKIE_REFRESH
from compute_space.core.auth.cookies import build_auth_cookies
from compute_space.core.auth.cookies import cleared_auth_cookies


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str | None = None,
    request: Request | None = None,
) -> Response:
    """Set zone_auth (and optionally zone_refresh) cookies on a Quart Response."""
    request_host = request.host if request else None
    for spec in build_auth_cookies(access_token, refresh_token, request_host=request_host):
        response.set_cookie(
            spec.name,
            spec.value,
            path=spec.path,
            domain=spec.domain,
            max_age=spec.max_age,
            secure=spec.secure,
            httponly=spec.http_only,
            samesite=spec.same_site.capitalize(),
        )
    return response


def clear_auth_cookies(response: Response, request: Request | None = None) -> Response:
    """Delete the auth cookies on a Quart Response."""
    request_host = request.host if request else None
    for spec in cleared_auth_cookies(request_host):
        response.delete_cookie(spec.name, path=spec.path, domain=spec.domain)
    return response
