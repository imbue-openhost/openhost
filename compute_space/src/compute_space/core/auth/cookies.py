"""Framework-neutral cookie helpers for auth.

The web layer translates ``CookieSpec`` values into framework-specific Response cookies.
"""

from datetime import timedelta
from typing import Literal

import attr

from compute_space.config import get_config

SameSite = Literal["lax", "strict", "none"]

ACCESS_TOKEN_EXPIRY = 3600  # 60 minutes
REFRESH_TOKEN_EXPIRY = 2592000  # 30 days
REFRESH_GRACE_PERIOD = timedelta(hours=2)

COOKIE_ACCESS = "zone_auth"
COOKIE_REFRESH = "zone_refresh"


@attr.s(auto_attribs=True, frozen=True)
class CookieSpec:
    """A framework-neutral description of a cookie to set or clear.

    For deletions, ``value`` is the empty string and ``max_age`` is ``0``; the web layer may
    still translate this to a framework-specific delete call if it prefers.
    """

    name: str
    value: str
    path: str = "/"
    domain: str | None = None
    max_age: int | None = None
    secure: bool = False
    http_only: bool = True
    same_site: SameSite = "lax"
    delete: bool = False


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


def build_auth_cookies(
    access_token: str,
    refresh_token: str | None = None,
    request_host: str | None = None,
) -> list[CookieSpec]:
    """Return cookie specs to set zone_auth (and optionally zone_refresh)."""
    domain = _cookie_domain(request_host)
    secure = get_config().tls_enabled
    cookies = [
        CookieSpec(
            name=COOKIE_ACCESS,
            value=access_token,
            path="/",
            domain=domain,
            max_age=ACCESS_TOKEN_EXPIRY + int(REFRESH_GRACE_PERIOD.total_seconds()),
            secure=secure,
            http_only=True,
            same_site="lax",
        )
    ]
    if refresh_token:
        cookies.append(
            CookieSpec(
                name=COOKIE_REFRESH,
                value=refresh_token,
                path="/",
                domain=domain,
                max_age=REFRESH_TOKEN_EXPIRY,
                secure=secure,
                http_only=True,
                same_site="lax",
            )
        )
    return cookies


def clear_auth_cookies_spec(request_host: str | None = None) -> list[CookieSpec]:
    """Return cookie specs to delete the auth cookies.

    Clears cookies both with the computed domain and without, to handle stale
    cookies that were set with a different Domain attribute (e.g. after switching
    TLS mode or changing zone_domain).
    """
    domain = _cookie_domain(request_host)
    specs = [
        CookieSpec(name=COOKIE_ACCESS, value="", path="/", domain=domain, delete=True),
        CookieSpec(name=COOKIE_REFRESH, value="", path="/", domain=domain, delete=True),
    ]
    if domain is not None:
        specs.append(CookieSpec(name=COOKIE_ACCESS, value="", path="/", domain=None, delete=True))
        specs.append(CookieSpec(name=COOKIE_REFRESH, value="", path="/", domain=None, delete=True))
    return specs
