from litestar.datastructures import Cookie

from compute_space.config import get_config
from compute_space.core.auth.jwt_tokens import ACCESS_TOKEN_EXPIRY
from compute_space.core.auth.jwt_tokens import REFRESH_GRACE_PERIOD
from compute_space.core.auth.jwt_tokens import REFRESH_TOKEN_EXPIRY

COOKIE_ACCESS = "zone_auth"
COOKIE_REFRESH = "zone_refresh"


def cookie_domain(request_host: str | None) -> str | None:
    """Return the cookie domain to set, or None to use the request host.

    When ``zone_domain`` is configured and the request comes from that zone (or
    a subdomain), set the cookie domain explicitly so the cookie is shared with
    app subdomains.  When the request comes from an unrelated host (e.g.
    127.0.0.1) return None so the browser doesn't reject the Set-Cookie.
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


def build_auth_cookies(
    access_token: str,
    refresh_token: str | None = None,
    request_host: str | None = None,
) -> list[Cookie]:
    """Build the cookies to set after login or token refresh."""
    domain = cookie_domain(request_host)
    secure = get_config().tls_enabled
    cookies = [
        Cookie(
            key=COOKIE_ACCESS,
            value=access_token,
            domain=domain,
            max_age=ACCESS_TOKEN_EXPIRY + int(REFRESH_GRACE_PERIOD.total_seconds()),
            secure=secure,
            httponly=True,
            samesite="lax",
        )
    ]
    if refresh_token:
        cookies.append(
            Cookie(
                key=COOKIE_REFRESH,
                value=refresh_token,
                domain=domain,
                max_age=REFRESH_TOKEN_EXPIRY,
                secure=secure,
                httponly=True,
                samesite="lax",
            )
        )
    return cookies
