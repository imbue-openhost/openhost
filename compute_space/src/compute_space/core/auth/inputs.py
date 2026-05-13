"""Framework-neutral inputs for authentication checks.

The web layer constructs an ``AuthInputs`` from its framework-specific request/websocket
object and passes it to ``get_current_user``.
"""

import attr


@attr.s(auto_attribs=True, frozen=True)
class AuthInputs:
    """Inputs needed to identify the current user from a request or websocket.

    ``method`` is the HTTP verb ("GET", "POST", ...) for HTTP requests, or "WS" for
    websockets. ``cookie_header`` is the raw ``Cookie:`` header value (used for duplicate
    detection); ``cookies`` is the parsed map.
    """

    cookies: dict[str, str]
    cookie_header: str
    auth_header: str
    method: str
    path: str
