"""Framework-neutral cookie specifications for zone auth.

The web layer translates ``CookieSpec`` into framework-specific Set-Cookie
headers (Litestar ``Cookie`` objects, raw header bytes, etc.).  Keeping this
module free of any web-framework imports preserves the rule that ``core/``
does not know about web frameworks.
"""

from typing import Literal

import attr

from compute_space.config import get_config
from compute_space.core.auth.tokens import ACCESS_TOKEN_EXPIRY
from compute_space.core.auth.tokens import REFRESH_GRACE_PERIOD
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY

COOKIE_ACCESS = "zone_auth"
COOKIE_REFRESH = "zone_refresh"

SameSite = Literal["lax", "strict", "none"]


@attr.s(auto_attribs=True, frozen=True)
class CookieSpec:
    name: str
    value: str
    path: str = "/"
    domain: str | None = None
    max_age: int | None = None
    secure: bool = False
    http_only: bool = True
    same_site: SameSite = "lax"


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
) -> list[CookieSpec]:
    """Build the cookies to set after login or token refresh."""
    domain = cookie_domain(request_host)
    secure = get_config().tls_enabled
    cookies = [
        CookieSpec(
            name=COOKIE_ACCESS,
            value=access_token,
            domain=domain,
            max_age=ACCESS_TOKEN_EXPIRY + int(REFRESH_GRACE_PERIOD.total_seconds()),
            secure=secure,
        )
    ]
    if refresh_token:
        cookies.append(
            CookieSpec(
                name=COOKIE_REFRESH,
                value=refresh_token,
                domain=domain,
                max_age=REFRESH_TOKEN_EXPIRY,
                secure=secure,
            )
        )
    return cookies


def cleared_auth_cookies(request_host: str | None = None) -> list[CookieSpec]:
    """Build deletion cookies for the auth cookies.

    Emits both with the computed domain and without, so we clear stale cookies
    that may have been set with a different ``Domain`` attribute (e.g. after
    switching TLS mode or changing zone_domain).
    """
    domain = cookie_domain(request_host)
    cookies: list[CookieSpec] = [
        CookieSpec(name=COOKIE_ACCESS, value="", domain=domain, max_age=0),
        CookieSpec(name=COOKIE_REFRESH, value="", domain=domain, max_age=0),
    ]
    if domain is not None:
        cookies.extend(
            [
                CookieSpec(name=COOKIE_ACCESS, value="", max_age=0),
                CookieSpec(name=COOKIE_REFRESH, value="", max_age=0),
            ]
        )
    return cookies
