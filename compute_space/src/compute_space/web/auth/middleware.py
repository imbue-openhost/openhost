"""Quart-side shim for unmigrated routes.

The Litestar `AuthMiddleware` wraps the outer ASGI app and populates
``scope["state"]["accessor"]`` and ``scope["state"]["origin"]`` for every
request, including ones that fall through to the mounted Quart sub-app.
These shims read that state so the existing `@login_required` /
`@app_auth_required` decorators on unmigrated blueprints keep working
unchanged — they just defer to the same checks `require_user` performs.
"""

import inspect
from collections.abc import Awaitable
from collections.abc import Callable
from functools import wraps
from typing import Any
from typing import cast

from litestar.types import Scope as LitestarScope
from quart import Request
from quart import jsonify
from quart import redirect
from quart import request
from quart.typing import ResponseReturnValue
from quart.wrappers import Websocket

from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.web.auth.auth import AppOrigin
from compute_space.web.auth.auth import RouterOrigin
from compute_space.web.auth.auth import get_accessor
from compute_space.web.auth.auth import get_origin
from compute_space.web.auth.guards import verify_app_auth
from compute_space.web.auth.guards import verify_owner_auth


def _scope(req_or_ws: Request | Websocket) -> LitestarScope:
    # Quart's request/websocket expose the underlying ASGI scope dict; cast it
    # to Litestar's Scope alias so the get_accessor / get_origin helpers (which
    # were written against Litestar's type alias) type-check cleanly.
    return cast(LitestarScope, req_or_ws.scope)


def get_current_user_from_request(req_or_ws: Request | Websocket) -> AuthenticatedUser | None:
    """Return the authenticated user iff this is a router-origin request."""
    scope = _scope(req_or_ws)
    accessor = get_accessor(scope)
    origin = get_origin(scope)
    if isinstance(accessor, AuthenticatedUser) and isinstance(origin, RouterOrigin):
        return accessor
    return None


def _app_from_origin(req_or_ws: Request | Websocket) -> str | None:
    """Resolve an app_id from a logged-in user calling from an app subdomain."""
    scope = _scope(req_or_ws)
    if not isinstance(get_accessor(scope), AuthenticatedUser):
        return None
    origin = get_origin(scope)
    if isinstance(origin, AppOrigin):
        return origin.app_id
    return None


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
            verify_owner_auth(cast(Any, request))
        except Exception:
            return _unauthorized()
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated


def app_auth_required(
    f: Callable[..., Any],
) -> Callable[..., Awaitable[ResponseReturnValue]]:
    """Identify the calling app and inject `app_id` as a kwarg.

    Two paths to an app identity:
      - Bearer app token: outer AuthMiddleware sets accessor=AuthenticatedApp.
      - Browser cookie from an app subdomain: accessor=AuthenticatedUser AND
        origin=AppOrigin (the subdomain is verified against the apps table by
        the outer middleware).
    """

    @wraps(f)
    async def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        try:
            verify_app_auth(cast(Any, request))
        except Exception:
            return jsonify({"error": "Missing or invalid authorization"}), 401
        scope = cast(LitestarScope, request.scope)
        accessor = get_accessor(scope)
        origin = get_origin(scope)
        if isinstance(accessor, AuthenticatedApp):
            kwargs["app_id"] = accessor.app_id
        elif isinstance(accessor, AuthenticatedUser) and isinstance(origin, AppOrigin):
            kwargs["app_id"] = origin.app_id
        else:
            return jsonify({"error": "Missing or invalid authorization"}), 401
        return await _ensure_async(f, *args, **kwargs)  # type: ignore[no-any-return]

    return decorated
