"""Auth cookie helpers (zone_auth, zone_refresh)."""

from quart import Request
from quart import Response

from compute_space.config import get_config
from compute_space.core.auth.tokens import ACCESS_TOKEN_EXPIRY
from compute_space.core.auth.tokens import REFRESH_GRACE_PERIOD
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY

COOKIE_ACCESS = "zone_auth"
COOKIE_REFRESH = "zone_refresh"


def _cookie_domain(request_host: str | None = None) -> str | None:
    """Return the cookie domain, or None to use the default (request host).

    When zone_domain is set (e.g. "user.dev-host.imbue.com") and the request
    is coming from that domain (or a subdomain of it), the cookie domain is
    set explicitly so cookies are shared with app subdomains like
    "dau-tracker.user.dev-host.imbue.com".

    When the request comes from a different host (e.g. 127.0.0.1 or localhost),
    returns None so the cookie is scoped to the request host — otherwise the
    browser would reject the Set-Cookie (domain mismatch).
    """
    zone = get_config().zone_domain
    if not zone:
        return None
    zone_no_port = zone.split(":")[0]
    if request_host:
        host_no_port = request_host.split(":")[0]
        if host_no_port != zone_no_port and not host_no_port.endswith("." + zone_no_port):
            return None
    return zone_no_port


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str | None = None,
    request: Request | None = None,
) -> Response:
    """Set zone_auth (and optionally zone_refresh) cookies."""
    request_host = request.host if request else None
    domain = _cookie_domain(request_host)
    response.set_cookie(
        COOKIE_ACCESS,
        access_token,
        path="/",
        domain=domain,
        httponly=True,
        secure=get_config().tls_enabled,
        samesite="Lax",
        max_age=ACCESS_TOKEN_EXPIRY + int(REFRESH_GRACE_PERIOD.total_seconds()),
    )
    if refresh_token:
        response.set_cookie(
            COOKIE_REFRESH,
            refresh_token,
            path="/",
            domain=domain,
            httponly=True,
            secure=get_config().tls_enabled,
            samesite="Lax",
            max_age=REFRESH_TOKEN_EXPIRY,
        )
    return response


def clear_auth_cookies(response: Response, request: Request | None = None) -> Response:
    """Delete auth cookies.

    Clears cookies both with the computed domain and without, to handle stale
    cookies that were set with a different Domain attribute (e.g. after switching
    TLS mode or changing zone_domain).
    """
    request_host = request.host if request else None
    domain = _cookie_domain(request_host)
    response.delete_cookie(COOKIE_ACCESS, path="/", domain=domain)
    response.delete_cookie(COOKIE_REFRESH, path="/", domain=domain)
    if domain is not None:
        response.delete_cookie(COOKIE_ACCESS, path="/")
        response.delete_cookie(COOKIE_REFRESH, path="/")
    return response
