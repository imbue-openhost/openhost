from quart import Blueprint
from quart import render_template
from quart import websocket

from compute_space.core import auth
from compute_space.core.terminal import handle_terminal_ws
from compute_space.web.middleware import login_required
from compute_space.web.routes.proxy import _parse_app_from_host
from compute_space.web.routes.proxy import ws_catch_all

pages_system_bp = Blueprint("pages_system", __name__)


@pages_system_bp.route("/system/")
@login_required
async def system_page() -> str:
    """Serve the System dashboard (security audit, storage, maintenance actions)."""
    return await render_template("system.html")


@pages_system_bp.route("/logs/")
@login_required
async def logs_page() -> str:
    """Serve the compute space logs page."""
    return await render_template("logs.html")


@pages_system_bp.route("/terminal/")
@login_required
async def terminal_page() -> str:
    """Serve the web terminal UI."""
    return await render_template("terminal.html")


@pages_system_bp.websocket("/terminal/ws")
async def terminal_ws() -> None:
    """WebSocket endpoint for the terminal PTY bridge.

    If the request is for an app subdomain, delegate to the proxy catch-all
    instead of opening a system terminal (otherwise this route captures all
    /terminal/ws traffic regardless of subdomain).
    """
    if _parse_app_from_host(websocket.host):
        await ws_catch_all("terminal/ws")
        return

    claims: dict[str, str] | None = auth.get_current_user_from_request(websocket)  # type: ignore[arg-type]
    if claims is None:
        return
    await handle_terminal_ws(websocket)
