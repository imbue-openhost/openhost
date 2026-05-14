"""Framework-neutral request inputs that auth needs.

Constructed by the web layer from a Litestar ``Request``/``WebSocket`` (or any
other ASGI request abstraction) and passed into ``get_current_user``.
"""

import attr


@attr.s(auto_attribs=True, frozen=True)
class AuthInputs:
    cookies: dict[str, str]
    cookie_header: str
    auth_header: str
    method: str  # "GET", "POST", ..., or "WS" for websockets
    path: str
