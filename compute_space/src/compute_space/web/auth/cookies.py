from litestar.datastructures import Cookie

from compute_space.config import get_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import SESSION_TTL_SECONDS


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


def clear_session_cookie(request_host: str | None = None) -> Cookie:
    """Build a cookie that overwrites the existing session cookie with a zero max-age."""
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        domain=cookie_domain(request_host),
        max_age=0,
        secure=get_config().tls_enabled,
        httponly=True,
        samesite="lax",
    )


def build_session_cookie(session_token: str, request_host: str | None = None) -> Cookie:
    """Build the cookie to set after login."""
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        domain=cookie_domain(request_host),
        max_age=SESSION_TTL_SECONDS,
        secure=get_config().tls_enabled,
        httponly=True,
        samesite="lax",
    )
