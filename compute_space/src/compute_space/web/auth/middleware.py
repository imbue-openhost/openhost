"""Quart-side shim for unmigrated routes."""

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

from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.web.auth.auth import AppOrigin
from compute_space.web.auth.auth import RouterOrigin
from compute_space.web.auth.auth import get_accessor
from compute_space.web.auth.auth import get_origin
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
