"""Litestar adapters that translate framework-neutral cookie specs into ``litestar.datastructures.Cookie``."""

from litestar.datastructures import Cookie

from compute_space.core.auth.cookies import CookieSpec
from compute_space.core.auth.cookies import build_auth_cookies
from compute_space.core.auth.cookies import clear_auth_cookies_spec


def _spec_to_cookie(spec: CookieSpec) -> Cookie:
    if spec.delete:
        return Cookie(
            key=spec.name,
            value="",
            path=spec.path,
            domain=spec.domain,
            max_age=0,
            expires=0,
            httponly=spec.http_only,
            secure=spec.secure,
            samesite=spec.same_site,
        )
    return Cookie(
        key=spec.name,
        value=spec.value,
        path=spec.path,
        domain=spec.domain,
        max_age=spec.max_age,
        httponly=spec.http_only,
        secure=spec.secure,
        samesite=spec.same_site,
    )


def auth_cookies(access_token: str, refresh_token: str | None, request_host: str | None) -> list[Cookie]:
    """Return ``Cookie`` objects ready to attach to a Litestar response."""
    return [
        _spec_to_cookie(spec) for spec in build_auth_cookies(access_token, refresh_token, request_host=request_host)
    ]


def cleared_auth_cookies(request_host: str | None) -> list[Cookie]:
    """Return ``Cookie`` objects for clearing the auth cookies on a Litestar response."""
    return [_spec_to_cookie(spec) for spec in clear_auth_cookies_spec(request_host=request_host)]
