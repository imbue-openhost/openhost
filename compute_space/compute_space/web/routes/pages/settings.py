from quart import Blueprint
from quart import render_template

from compute_space.web.middleware import login_required

pages_settings_bp = Blueprint("pages_settings", __name__)


@pages_settings_bp.route("/settings")
@login_required
async def settings_page() -> str:
    return await render_template("settings.html")
