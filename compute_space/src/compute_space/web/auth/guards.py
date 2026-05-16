from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler

from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.web.auth.auth import RouterOrigin
from compute_space.web.auth.auth import get_accessor
from compute_space.web.auth.auth import get_origin

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def require_user(connection: _AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Require a user-equivalent caller: either a logged-in user (identified via session cookie) or an owner-API token.

    Returns None if the caller is authorized, otherwise raises NotAuthorizedException.

    Requests with user cookies but non-router origins (e.g. app subdomains) are rejected, as they could have come from untrusted app js.
    Untrusted app js can't spoof the origin, as this is enforced by the user's browser.
    """

    accessor = get_accessor(connection.scope)
    origin = get_origin(connection.scope)

    if isinstance(accessor, AuthenticatedUser):
        if not isinstance(origin, RouterOrigin):
            raise NotAuthorizedException(detail="user authentication only valid for router-origin requests")
        return  # valid
    if isinstance(accessor, AuthenticatedAPIKey):
        return  # valid

    raise NotAuthorizedException(detail="User authentication required")
