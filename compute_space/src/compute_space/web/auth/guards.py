from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler

from compute_space.config import get_config
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.web.auth.auth import _get_connection_origin
from compute_space.web.auth.auth import get_accessor

_AnyConnection = ASGIConnection[Any, Any, Any, Any]


def verify_owner_auth(connection: _AnyConnection) -> None:
    """Verify that the request is authenticated as an "owner" (either a user or an API key, with valid Origin).

    returns if authed; raises NotAuthorizedException if not authenticated.
    """
    accessor = get_accessor(connection.scope)
    origin = _get_connection_origin(connection)

    if isinstance(accessor, AuthenticatedUser):
        if origin is not None and origin != get_config().zone_domain:
            # if origin is set (it is set on all browser cross-origin requests and cannot be forged by js),
            # it must match the router zone to be considered from the router itself.
            # otherwise apps could contain JS that makes requests using the user's auth to the router.
            raise NotAuthorizedException(detail="user authentication only valid for router-origin requests")
        # origin is not set on normal same-origin GETs, for example, so we allow these.
        return
    if isinstance(accessor, AuthenticatedAPIKey):
        # API key requests won't come from untrusted JS, so can be trusted regardless of origin.
        return
    raise NotAuthorizedException(detail="User or API key authentication required")


def verify_app_auth(connection: _AnyConnection) -> None:
    """Verify that the request is authenticated as an "app" (either client-side, from app js, or server-side, from an app token).

    returns if authed; raises NotAuthorizedException if not authenticated.
    """
    accessor = get_accessor(connection.scope)
    origin = _get_connection_origin(connection)

    if isinstance(accessor, AuthenticatedUser):
        if origin is not None and get_app_from_hostname(origin) is not None:
            # requests from app js come from the user's browser with the user's auth.
            # Origin will always be set by the browser on these cross-origin requests.
            return
    if isinstance(accessor, AuthenticatedApp):
        # server-side app requests.
        return
    raise NotAuthorizedException(detail="app authentication required")


def require_owner_auth(connection: _AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_owner_auth to be used as a route guard."""
    verify_owner_auth(connection)
