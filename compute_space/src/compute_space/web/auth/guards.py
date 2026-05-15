from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler

from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _get_accessor(connection: _AnyConnection) -> AuthenticatedAccessor | None:
    state = connection.scope.get("state") or {}
    return state.get("accessor")


def require_authenticated(connection: _AnyConnection, _route_handler: BaseRouteHandler) -> None:
    if _get_accessor(connection) is None:
        raise NotAuthorizedException(detail="Authentication required")


def require_user(connection: _AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Require a user-equivalent caller: either a logged-in user (JWT) or an owner-API token."""
    accessor = _get_accessor(connection)
    if not isinstance(accessor, (AuthenticatedUser, AuthenticatedAPIKey)):
        raise NotAuthorizedException(detail="User authentication required")


def require_app(connection: _AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Require an app caller: Bearer app-token or Origin-subdomain."""
    accessor = _get_accessor(connection)
    if not isinstance(accessor, AuthenticatedApp):
        raise NotAuthorizedException(detail="App authentication required")
