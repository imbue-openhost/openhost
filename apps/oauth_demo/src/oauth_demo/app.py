from oauth_demo.client_demo import client_bp
from oauth_demo.oauth import SCOPE_MAP
from oauth_demo.oauth import AuthRedirectRequired
from oauth_demo.oauth import OAuthError
from oauth_demo.oauth import OAuthServiceUnavailable
from oauth_demo.oauth import get_accounts
from oauth_demo.oauth import get_oauth_token
from oauth_demo.oauth import set_mock_oauth_url
from oauth_demo.oauth import set_mock_provider_api_url
from oauth_demo.server_demo import server_bp
from quart import Quart
from quart import jsonify
from quart import render_template
from quart import request

app = Quart(__name__)
app.register_blueprint(client_bp)
app.register_blueprint(server_bp)


@app.route("/")
async def landing():
    return await render_template("landing.html")


@app.route("/test/set-mock-url", methods=["POST"])
async def test_set_mock_url():
    data = await request.get_json()
    url = data.get("url") if data else None
    if "url" in (data or {}):
        set_mock_oauth_url(url)
    provider_api_url = data.get("provider_api_url") if data else None
    if provider_api_url:
        set_mock_provider_api_url(provider_api_url)
    return jsonify({"ok": True, "mock_url": url})


@app.route("/test/token", methods=["POST"])
async def test_get_token():
    data = await request.get_json()
    provider = data.get("provider", "google")
    scopes = data.get("scopes") or SCOPE_MAP.get(provider, [])
    account = data.get("account", "default")
    return_to = data.get("return_to", "")
    try:
        token = await get_oauth_token(provider, scopes, account, return_to=return_to)
        return jsonify({"access_token": token})
    except AuthRedirectRequired as e:
        return jsonify({"redirect_url": e.url}), 401
    except OAuthServiceUnavailable as e:
        return jsonify({"error": "service_unavailable", "message": str(e)}), 503
    except OAuthError as e:
        return jsonify({"error": "oauth_error", "message": str(e)}), 502


@app.route("/test/accounts", methods=["POST"])
async def test_get_accounts():
    data = await request.get_json()
    provider = data.get("provider", "google")
    try:
        accounts = await get_accounts(provider)
        return jsonify({"accounts": accounts})
    except OAuthServiceUnavailable as e:
        return jsonify({"error": "service_unavailable", "message": str(e)}), 503
    except OAuthError as e:
        return jsonify({"error": "oauth_error", "message": str(e)}), 502
