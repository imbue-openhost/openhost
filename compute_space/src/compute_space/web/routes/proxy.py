from quart import Blueprint
from quart import Response
from quart import current_app
from quart import g
from quart import redirect
from quart import request
from quart import websocket
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core import auth
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import is_public_path
from compute_space.core.apps import parse_app_from_host
from compute_space.web.auth.cookies import set_auth_cookies
from compute_space.web.auth.inputs import auth_inputs_from_request
from compute_space.web.auth.middleware import _try_refresh
from compute_space.web.proxy import proxy_request
from compute_space.web.proxy import ws_proxy

proxy_bp = Blueprint("proxy", __name__)


def _identity_headers(claims: dict[str, str] | None) -> dict[str, str]:
    if claims and claims.get("sub") == "owner":
        return {"X-OpenHost-Is-Owner": "true"}
    return {}


# ─── Subdomain Reverse Proxy ───
#
# Both hooks fire before route matching is consulted (for WS, the routing
# exception is only raised inside ``dispatch_websocket``, after preprocessing).
# Returning a non-None value short-circuits dispatch, so no catch-all route is
# needed and specific routes (e.g. /api/apps, /terminal/ws) don't capture
# traffic destined for an app subdomain.


@proxy_bp.before_app_request
async def _app_subdomain_routing() -> ResponseReturnValue | None:
    """Route HTTP requests for app subdomains directly to the app."""
    if not getattr(current_app, "_owner_verified", False):
        return None
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    app_subdomain = parse_app_from_host(request.host)
    if not app_subdomain:
        return None
    app_row = find_app_by_name(app_subdomain)
    if not app_row:
        return Response(f"App '{app_subdomain}' not found", status=404)

    new_access_token = None
    claims = auth.get_current_user(auth_inputs_from_request(request))
    if claims is None:
        claims = _try_refresh()
        if claims:
            new_access_token = getattr(g, "new_access_token", None)

    if claims is None and not is_public_path(app_row, request.path):
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        return redirect(f"{proto}://{get_config().zone_domain}/login")

    # Use a longer timeout for large requests (e.g. migration data transfers)
    content_length = request.content_length or 0
    timeout = 600 if content_length > 10 * 1024 * 1024 else 30

    response = await proxy_request(
        request,
        app_row["local_port"],
        extra_headers=_identity_headers(claims),  # type: ignore[arg-type]
        timeout=timeout,
    )

    if new_access_token:
        set_auth_cookies(
            response,
            new_access_token,
            request.cookies.get(auth.COOKIE_REFRESH),
            request=request,
        )

    return response


@proxy_bp.before_app_websocket
async def _app_subdomain_routing_ws() -> str | None:
    """WS analog of ``_app_subdomain_routing``."""
    if not getattr(current_app, "_owner_verified", False):
        return None
    app_subdomain = parse_app_from_host(websocket.host)
    if not app_subdomain:
        return None
    app_row = find_app_by_name(app_subdomain)
    if app_row and app_row["status"] in ("running", "starting"):
        claims = auth.get_current_user(auth_inputs_from_request(websocket))
        if claims is not None or is_public_path(app_row, websocket.path):
            await ws_proxy(app_row["local_port"], websocket, identity_headers=_identity_headers(claims))
    return ""  # non-None to skip dispatch
