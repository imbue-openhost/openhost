"""Mock OAuth service and provider (Quart app).

Serves two roles:
1. **Service API** (used when oauth-demo bypasses the router via set-mock-url):
   POST /token, POST /accounts, POST|GET /authorize-complete
2. **OAuth provider** (used for Playwright e2e tests through the real flow):
   GET /authorize — HTML account picker page
   POST /oauth/token — authorization code exchange
   GET /userinfo — returns account identity
"""

import secrets
from html import escape
from urllib.parse import urlencode

from quart import Quart
from quart import Response
from quart import jsonify
from quart import request

app = Quart(__name__)

# ─── State ───

tokens: dict[str, dict[str, str]] = {}
authorize_base_url: str = ""
available_accounts: list[str] = ["alice@example.com", "bob@example.com"]
authorization_codes: dict[str, dict] = {}
token_to_account: dict[str, str] = {}


def reset() -> None:
    tokens.clear()
    authorization_codes.clear()
    token_to_account.clear()


def add_token(provider: str, scopes: str, account: str, access_token: str) -> None:
    key = f"{provider}:{scopes}:{account}"
    tokens[key] = {"access_token": access_token, "account": account}
    token_to_account[access_token] = account


# ─── OAuth provider endpoints (for Playwright tests) ───


@app.route("/authorize")
async def authorize_page():
    """Render an account picker page, like Google's 'Choose an account'."""
    redirect_uri = request.args.get("redirect_uri", "")
    scope = request.args.get("scope", "")
    state = request.args.get("state", "")

    links = []
    for account in available_accounts:
        code = secrets.token_urlsafe(16)
        authorization_codes[code] = {
            "account": account,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
        callback = redirect_uri + "?" + urlencode({"code": code, "state": state, "scope": scope})
        links.append(
            f'<a href="{escape(callback)}" class="account" data-testid="account-{escape(account)}">'
            f"{escape(account)}</a>"
        )

    html = (
        "<!DOCTYPE html><html><body>"
        "<h1>Choose an account</h1>"
        '<div class="accounts">' + "<br>".join(links) + "</div>"
        "</body></html>"
    )
    return Response(html, content_type="text/html")


@app.route("/oauth/token", methods=["POST"])
async def code_exchange():
    """Exchange authorization code for access token (form-encoded POST)."""
    form = await request.form
    code = form.get("code", "")

    flow = authorization_codes.pop(code, None)
    if not flow:
        return jsonify({"error": "invalid_grant"}), 400

    access_token = f"mock_token_{flow['account']}"
    token_to_account[access_token] = flow["account"]

    return jsonify(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )


@app.route("/userinfo")
async def userinfo():
    """Return account identity for an access token."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    account = token_to_account.get(token)
    if not account:
        return jsonify({"error": "invalid_token"}), 401
    return jsonify({"email": account, "login": account})


# ─── Service API endpoints (for API-based tests) ───


@app.route("/token", methods=["POST"])
async def service_token():
    body = await request.get_json()
    provider = body.get("provider", "")
    scopes = body.get("scopes", [])
    account = body.get("account", "default")
    return_to = body.get("return_to", "/")
    scopes_key = " ".join(sorted(scopes))

    key = f"{provider}:{scopes_key}:{account}"
    token_info = tokens.get(key)

    if not token_info and account == "default":
        prefix = f"{provider}:{scopes_key}:"
        matches = {k: v for k, v in tokens.items() if k.startswith(prefix)}
        if len(matches) == 1:
            token_info = next(iter(matches.values()))

    if token_info:
        return jsonify(
            {
                "access_token": token_info["access_token"],
                "expires_at": None,
                "token_type": "Bearer",
            }
        )

    auth_url = f"{authorize_base_url}/authorize-complete?" + urlencode(
        {"provider": provider, "scopes": scopes_key, "account": account, "return_to": return_to}
    )
    return jsonify({"status": "authorization_required", "authorize_url": auth_url}), 401


@app.route("/accounts", methods=["POST"])
async def service_accounts():
    body = await request.get_json()
    provider = body.get("provider", "")
    scopes = body.get("scopes", [])
    scopes_key = " ".join(sorted(scopes))
    prefix = f"{provider}:{scopes_key}:"
    accounts = [v["account"] for k, v in sorted(tokens.items()) if k.startswith(prefix)]
    return jsonify({"accounts": accounts})


@app.route("/authorize-complete", methods=["GET", "POST"])
async def authorize_complete():
    if request.method == "POST":
        body = await request.get_json()
    else:
        body = dict(request.args)

    provider = body.get("provider", "")
    scopes_key = body.get("scopes", "")
    account = body.get("account", "default")
    access_token = body.get("access_token", "")

    if not access_token:
        access_token = f"mock_token_{provider}_{account}"

    key = f"{provider}:{scopes_key}:{account}"
    tokens[key] = {"access_token": access_token, "account": account}
    token_to_account[access_token] = account
    return jsonify({"ok": True, "account": account})
