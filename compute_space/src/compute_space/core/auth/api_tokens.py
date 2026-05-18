"""Compat shim for the legacy ``core.auth.api_tokens`` module location.

The unmigrated ``web/routes/services_v2.py`` still imports
``resolve_app_from_token`` from here.  In the new architecture the outer
``AuthMiddleware`` validates app tokens up-front and exposes the
resolved ``AuthenticatedApp`` on ``scope["state"]``; this wrapper exists
so that legacy WebSocket helpers (which take a raw bearer string) keep
working until services_v2 is ported.
"""

from compute_space.core.auth.auth import validate_app_token
from compute_space.db import get_db


def resolve_app_from_token(token: str) -> str | None:
    accessor = validate_app_token(token, get_db())
    return accessor.app_id if accessor else None
