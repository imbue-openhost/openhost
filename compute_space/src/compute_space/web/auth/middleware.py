from typing import Any

from litestar import Request
from litestar import Response
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect

from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.db import get_db


async def provide_accessor(request: Request[Any, Any, Any]) -> AuthenticatedAccessor:
    """Litestar dependency: return the AuthenticatedAccessor populated by AuthAccessorMiddleware."""
    state = request.scope.get("state") or {}
    accessor = state.get("accessor")
    if accessor is None:
        raise NotAuthorizedException(detail="Authentication required")
    return accessor


def login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /setup or /login; JSON clients get 401."""
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        claim = request.query_params.get("claim", "")
        target = f"/setup?claim={claim}" if claim else "/setup"
    else:
        target = "/login"
    return Redirect(path=target)
