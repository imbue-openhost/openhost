from litestar.datastructures import Cookie

from compute_space.config import get_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import SESSION_TTL_SECONDS


def clear_session_cookie(cookie_domain: str) -> Cookie:
    """Build a cookie that overwrites the existing session cookie with a zero max-age."""
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        domain=cookie_domain,
        max_age=0,
        secure=get_config().tls_enabled,
        httponly=True,
        samesite="lax",
    )


def build_session_cookie(session_token: str, cookie_domain: str) -> Cookie:
    """Build the cookie to set after login."""
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        domain=cookie_domain,
        max_age=SESSION_TTL_SECONDS,
        secure=get_config().tls_enabled,
        # makes it so this cookie may not be read by client-side JavaScript.
        httponly=True,
        samesite="lax",
    )
