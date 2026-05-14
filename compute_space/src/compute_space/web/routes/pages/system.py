from quart import Blueprint
from quart import render_template
from quart import websocket

from compute_space.core.auth.identity import get_current_user_from_request
from compute_space.core.terminal import handle_terminal_ws
from compute_space.web.auth.middleware import login_required

pages_system_bp = Blueprint("pages_system", __name__)


@pages_system_bp.route("/system/")
@login_required
async def system_page() -> str:
    """Serve the System page (security audit, storage, maintenance actions)."""
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
    """WebSocket endpoint for the terminal PTY bridge."""
    claims: dict[str, str] | None = get_current_user_from_request(websocket)
    if claims is None:
        return
    await handle_terminal_ws(websocket)
