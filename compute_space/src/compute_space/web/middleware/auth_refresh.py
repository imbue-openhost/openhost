"""ASGI middleware that attaches refreshed-auth cookies to outgoing HTTP responses.

Routes/dependencies place the new tokens in ``scope["state"]`` (under ``new_access_token`` /
``refresh_token``); this middleware appends the corresponding ``Set-Cookie`` headers to the
``http.response.start`` message so downstream framework code never has to mention cookies.
"""

from typing import Any

from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.auth.cookies import CookieSpec
from compute_space.core.auth.cookies import build_auth_cookies


def _serialize_cookie(spec: CookieSpec) -> bytes:
    parts = [f"{spec.name}={spec.value}"]
    if spec.path:
        parts.append(f"Path={spec.path}")
    if spec.domain:
        parts.append(f"Domain={spec.domain}")
    if spec.max_age is not None:
        parts.append(f"Max-Age={spec.max_age}")
    if spec.same_site:
        parts.append(f"SameSite={spec.same_site.capitalize()}")
    if spec.secure:
        parts.append("Secure")
    if spec.http_only:
        parts.append("HttpOnly")
    return "; ".join(parts).encode("latin-1")


def _request_host(scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"host":
            return value.decode("latin-1")
    return ""


class AuthRefreshMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if str(scope["type"]) != "http":
            await self.app(scope, receive, send)
            return

        host = _request_host(scope)

        async def wrapped_send(message: Message) -> None:
            if str(message["type"]) == "http.response.start":
                state = scope.get("state") or {}
                new_access = state.get("new_access_token")
                if new_access:
                    refresh = state.get("refresh_token")
                    cookies = build_auth_cookies(new_access, refresh, request_host=host)
                    headers_obj: Any = message.get("headers", [])
                    headers = list(headers_obj) if headers_obj else []
                    for spec in cookies:
                        if spec.delete:
                            continue
                        headers.append((b"set-cookie", _serialize_cookie(spec)))
                    message["headers"] = headers  # type: ignore[typeddict-unknown-key]
            await send(message)

        await self.app(scope, receive, wrapped_send)
