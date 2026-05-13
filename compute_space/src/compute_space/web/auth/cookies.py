"""Quart adapters that translate framework-neutral cookie specs into Response cookies."""

from quart import Request
from quart import Response

from compute_space.core.auth.cookies import CookieSpec
from compute_space.core.auth.cookies import build_auth_cookies
from compute_space.core.auth.cookies import clear_auth_cookies_spec


def _apply_cookie(response: Response, spec: CookieSpec) -> None:
    if spec.delete:
        response.delete_cookie(spec.name, path=spec.path, domain=spec.domain)
        return
    response.set_cookie(
        spec.name,
        spec.value,
        path=spec.path,
        domain=spec.domain,
        max_age=spec.max_age,
        httponly=spec.http_only,
        secure=spec.secure,
        samesite=spec.same_site.capitalize(),
    )


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str | None = None,
    request: Request | None = None,
) -> Response:
    """Set zone_auth (and optionally zone_refresh) cookies on a Quart response."""
    request_host = request.host if request else None
    for spec in build_auth_cookies(access_token, refresh_token, request_host=request_host):
        _apply_cookie(response, spec)
    return response


def clear_auth_cookies(response: Response, request: Request | None = None) -> Response:
    """Delete the auth cookies on a Quart response."""
    request_host = request.host if request else None
    for spec in clear_auth_cookies_spec(request_host=request_host):
        _apply_cookie(response, spec)
    return response
