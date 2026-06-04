import secrets
from html import escape
from urllib.parse import urlencode

from quart import Quart
from quart import Response
from quart import jsonify
from quart import request

app = Quart(__name__)

# ─── State ───

authorization_codes: dict[str, dict] = {}
token_to_account: dict[str, str] = {}

AVAILABLE_ACCOUNTS = ["alice@example.com", "bob@example.com"]
ACCOUNT_TOKENS: dict[str, str] = {
    "alice@example.com": "mock_alice_token_abc123",
    "bob@example.com": "mock_bob_token_def456",
}
MOCK_EMAILS = [
    {"subject": "Welcome to the mock", "from": "noreply@mock.example.com"},
    {"subject": "Your invoice is ready", "from": "billing@mock.example.com"},
    {"subject": "New login from Chrome", "from": "security@mock.example.com"},
]


@app.route("/health")
async def health():
    return jsonify({"status": "ok"})


@app.route("/reset", methods=["POST"])
async def reset():
    authorization_codes.clear()
    token_to_account.clear()
    return jsonify({"ok": True})


@app.route("/authorize")
async def authorize_page():
    """Render an account picker page, like Google's 'Choose an account'."""
    redirect_uri = request.args.get("redirect_uri", "")
    scope = request.args.get("scope", "")
    state = request.args.get("state", "")

    links = []
    for account in AVAILABLE_ACCOUNTS:
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

    access_token = ACCOUNT_TOKENS.get(flow["account"], f"mock_token_{flow['account']}")
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


@app.route("/oauth/revoke", methods=["POST"])
async def revoke():
    """Revoke an access token."""
    form = await request.form
    token = form.get("token", "")
    token_to_account.pop(token, None)
    return jsonify({"ok": True})


@app.route("/api/emails")
async def api_emails():
    """Protected resource: returns fake emails if the bearer token is valid."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    account = token_to_account.get(token)
    if not account:
        return jsonify({"error": "invalid_token"}), 401
    return jsonify({"account": account, "emails": MOCK_EMAILS})
