"""Quart-side shim for unmigrated routes.

Unmigrated blueprints still call ``@login_required`` and reach for
``get_current_user_from_request`` on websockets.  Both defer to the
framework-neutral helpers in ``web/auth/auth.py`` so the Quart and
Litestar sides share one auth policy.
"""

import inspect
from collections.abc import Awaitable
from collections.abc import Callable
from functools import wraps
from typing import Any
from typing import cast

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from quart import Request
from quart import jsonify
from quart import redirect
from quart import request
from quart.typing import ResponseReturnValue
from quart.wrappers import Websocket

from compute_space.config import get_config
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.db import get_db
from compute_space.web.auth.auth import AnyConnection
from compute_space.web.auth.auth import authenticate
from compute_space.web.auth.auth import get_connection_origin
from compute_space.web.auth.auth import verify_owner_auth


def _as_litestar_connection(req_or_ws: Request | Websocket) -> AnyConnection:
    """Wrap a Quart Request/Websocket as a Litestar ``ASGIConnection``.

    The shared auth helpers in ``web/auth/auth.py`` accept any Litestar
    ASGIConnection and use its URL/header/cookie accessors.  Quart's Request
    has a similar surface but with subtly different types (e.g. ``base_url``
    is ``str`` vs a URL object), so passing it raw breaks the helpers.
    Constructing an ASGIConnection from the underlying ASGI scope gives the
    helpers exactly the shape they expect.
    """
    # Hypercorn and Litestar both type their ASGI scopes as TypedDicts derived
    # from the same ASGI spec; the runtime objects are interchangeable.
    return ASGIConnection(cast(Any, req_or_ws.scope))


def get_current_user_from_request(req_or_ws: Request | Websocket) -> AuthenticatedUser | None:
    """Return the authenticated user iff this is a router-origin request.

    Mirrors ``verify_owner_auth`` for the WebSocket handshake path in
    pages/system.py — the same router-origin policy, just returning the
    accessor instead of raising.
    """
    connection = _as_litestar_connection(req_or_ws)
    accessor = authenticate(connection, db=get_db())
    if not isinstance(accessor, AuthenticatedUser):
        return None
    origin = get_connection_origin(connection)
    # Origin is None for many same-origin requests (browsers don't always set
    # it on GET); only reject when it's set AND doesn't match the zone.
    if origin is not None and origin != get_config().zone_domain:
        return None
    return accessor


async def _ensure_async(f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = f(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _wants_json() -> bool:
    return "application/json" in request.headers.get("Accept", "")


def _unauthorized(detail: str = "User authentication required") -> ResponseReturnValue:
    if _wants_json():
        return jsonify({"error": detail}), 401
    return redirect("/login")


def login_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    """Defer to ``verify_owner_auth`` so unmigrated Quart blueprints use the
    same policy as the Litestar ``require_owner_auth`` guard."""

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        try:
            verify_owner_auth(_as_litestar_connection(request))
        except NotAuthorizedException:
            return _unauthorized()
        # Any other exception is a real bug — let it propagate so it gets
        # logged instead of silently turning into a /login redirect.
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated
