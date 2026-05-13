from typing import Any

from litestar import WebSocket
from litestar import get
from litestar import websocket
from litestar.response import Template

from compute_space.core import auth
from compute_space.core.terminal import handle_terminal_ws
from compute_space.web.auth.inputs import auth_inputs_from_connection


@get("/system/", sync_to_thread=False)
def system_page(user: dict[str, Any]) -> Template:
    return Template(template_name="system.html")


@get("/logs/", sync_to_thread=False)
def logs_page(user: dict[str, Any]) -> Template:
    return Template(template_name="logs.html")


@get("/terminal/", sync_to_thread=False)
def terminal_page(user: dict[str, Any]) -> Template:
    return Template(template_name="terminal.html")


class _WsAdapter:
    """Bridges Litestar's WebSocket API to the simpler send/receive Protocol used by handle_terminal_ws."""

    def __init__(self, ws: WebSocket[Any, Any, Any]) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive(self) -> bytes | str:
        msg: Any = await self._ws.receive()
        bytes_payload = msg.get("bytes")
        text_payload = msg.get("text")
        if bytes_payload is not None:
            return bytes_payload  # type: ignore[no-any-return]
        if text_payload is not None:
            return text_payload  # type: ignore[no-any-return]
        return b""


@websocket("/terminal/ws")
async def terminal_ws(socket: WebSocket[Any, Any, Any]) -> None:
    claims = auth.get_current_user(auth_inputs_from_connection(socket))
    if claims is None:
        await socket.close(code=4401)
        return
    await socket.accept()
    await handle_terminal_ws(_WsAdapter(socket))


pages_system_routes = [system_page, logs_page, terminal_page, terminal_ws]
