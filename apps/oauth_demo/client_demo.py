"""Client-side OAuth demo — single-page app, JS handles everything."""

from quart import Blueprint
from quart import render_template

client_bp = Blueprint("client", __name__, url_prefix="/client")


@client_bp.route("/")
async def index():
    return await render_template("client/index.html")


@client_bp.route("/oauth-complete")
async def oauth_complete():
    return await render_template("client/oauth_complete.html")
