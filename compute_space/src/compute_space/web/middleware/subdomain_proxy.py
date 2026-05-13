"""ASGI middleware that proxies app-subdomain requests directly to backend ports.

If the host doesn't parse as an app subdomain, or the owner hasn't been verified yet, the request is passed through to
the regular Litestar router.
"""

import hashlib
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import cast

from litestar import WebSocket
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.config import get_config
from compute_space.core import auth
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import is_public_path
from compute_space.core.apps import parse_app_from_host
from compute_space.core.auth.inputs import AuthInputs
from compute_space.db import get_db
from compute_space.web.proxy import ProxiedResponse
from compute_space.web.proxy import _scope_cookies
from compute_space.web.proxy import _scope_host
from compute_space.web.proxy import proxy_request_raw
from compute_space.web.proxy import ws_proxy


def _identity_headers(claims: dict[str, str] | None) -> dict[str, str]:
    if claims and claims.get("sub") == "owner":
        return {"X-OpenHost-Is-Owner": "true"}
    return {}


def _auth_inputs_from_scope(scope: Scope) -> AuthInputs:
    cookies = _scope_cookies(scope)
    cookie_header = ""
    auth_header = ""
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            cookie_header = value.decode("latin-1")
        elif key.lower() == b"authorization":
            auth_header = value.decode("latin-1")
    raw_method = scope.get("method")
    if raw_method is not None:
        method = str(raw_method)
    else:
        method = "WS" if str(scope.get("type", "")) == "websocket" else "GET"
    return AuthInputs(
        cookies=cookies,
        cookie_header=cookie_header,
        auth_header=auth_header,
        method=method,
        path=scope.get("path", "/"),
    )


def _content_length(scope: Scope) -> int:
    for key, value in scope.get("headers", []):
        if key.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _is_websocket_upgrade(scope: Scope) -> bool:
    for key, value in scope.get("headers", []):
        if key.lower() == b"upgrade" and value.lower() == b"websocket":
            return True
    return False


def _try_refresh_for_scope(scope: Scope) -> dict[str, Any] | None:
    cookies = _scope_cookies(scope)
    refresh_tok = cookies.get(auth.COOKIE_REFRESH)
    if not refresh_tok:
        return None
    expired_jwt = cookies.get(auth.COOKIE_ACCESS)
    if not expired_jwt:
        return None
    expired_claims = auth.decode_access_token_allow_expired(expired_jwt)
    if expired_claims is None:
        return None
    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    db = get_db()
    rt = db.execute(
        "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None:
        return None
    expires_at = datetime.fromisoformat(rt["expires_at"])
    if expires_at < datetime.now(UTC):
        return None
    new_access_token = auth.create_access_token(expired_claims["sub"])
    state = scope.setdefault("state", {})
    state["new_access_token"] = new_access_token
    state["refresh_token"] = refresh_tok
    return auth.decode_access_token(new_access_token)


def _owner_verified(scope: Scope) -> bool:
    app = scope.get("app")
    if app is None:
        return False
    state = getattr(app, "state", None)
    if state is None:
        return False
    return bool(getattr(state, "owner_verified", False))


async def _send_proxied(send: Send, proxied: ProxiedResponse) -> None:
    headers: list[tuple[bytes, bytes]] = []
    if proxied.media_type:
        headers.append((b"content-type", proxied.media_type.encode("latin-1")))
    for k, v in proxied.headers:
        headers.append((k.encode("latin-1"), v.encode("latin-1")))
    await send(
        cast(
            Message,
            {
                "type": "http.response.start",
                "status": proxied.status_code,
                "headers": headers,
            },
        )
    )
    await send(cast(Message, {"type": "http.response.body", "body": proxied.body}))


async def _send_simple(send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    base_headers: list[tuple[bytes, bytes]] = [(b"content-type", b"text/plain; charset=utf-8")]
    if headers:
        base_headers.extend(headers)
    await send(cast(Message, {"type": "http.response.start", "status": status, "headers": base_headers}))
    await send(cast(Message, {"type": "http.response.body", "body": body}))


class SubdomainProxyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if not _owner_verified(scope):
            await self.app(scope, receive, send)
            return

        host = _scope_host(scope)
        app_subdomain = parse_app_from_host(host)
        if not app_subdomain:
            await self.app(scope, receive, send)
            return

        if scope_type == "http":  # type: ignore[comparison-overlap]
            if _is_websocket_upgrade(scope):
                await self.app(scope, receive, send)
                return

            app_row = find_app_by_name(app_subdomain)
            if not app_row:
                await _send_simple(send, 404, f"App '{app_subdomain}' not found".encode())
                return

            claims = auth.get_current_user(_auth_inputs_from_scope(scope))
            if claims is None:
                claims = _try_refresh_for_scope(scope)

            path = scope.get("path", "/")
            if claims is None and not is_public_path(app_row, path):
                proto_header = None
                for key, value in scope.get("headers", []):
                    if key.lower() == b"x-forwarded-proto":
                        proto_header = value.decode("latin-1")
                        break
                proto = proto_header or scope.get("scheme", "http")
                redirect_url = f"{proto}://{get_config().zone_domain}/login"
                await send(
                    cast(
                        Message,
                        {
                            "type": "http.response.start",
                            "status": 302,
                            "headers": [(b"location", redirect_url.encode("latin-1"))],
                        },
                    )
                )
                await send(cast(Message, {"type": "http.response.body", "body": b""}))
                return

            content_length = _content_length(scope)
            timeout = 600 if content_length > 10 * 1024 * 1024 else 30

            extra: dict[str, str | None] = dict(_identity_headers(claims))
            proxied = await proxy_request_raw(
                scope,
                receive,
                app_row["local_port"],
                extra_headers=extra,
                timeout=timeout,
            )
            await _send_proxied(send, proxied)
            return

        # websocket
        app_row = find_app_by_name(app_subdomain)
        if app_row and app_row["status"] in ("running", "starting"):
            claims = auth.get_current_user(_auth_inputs_from_scope(scope))
            path = scope.get("path", "/")
            if claims is not None or is_public_path(app_row, path):
                ws = WebSocket[Any, Any, Any](scope, receive, send)
                await ws_proxy(app_row["local_port"], ws, identity_headers=_identity_headers(claims))
                return
        await send(cast(Message, {"type": "websocket.close", "code": 1008}))
