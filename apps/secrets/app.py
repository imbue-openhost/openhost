import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from oauth import PROVIDERS
from oauth import USERINFO_URLS
from oauth import active_device_flows
from oauth import build_auth_url
from oauth import create_device_flow
from oauth import exchange_code
from oauth import fetch_account_identity
from oauth import normalize_scopes
from oauth import pending_auth_flows
from oauth import refresh_access_token
from oauth import revoke_token
from oauth import start_device_flow
from quart import Quart
from quart import Response
from quart import jsonify
from quart import redirect
from quart import render_template
from quart import request

app = Quart(__name__)

DB_PATH = os.environ.get("OPENHOST_SQLITE_MAIN", "/data/app_data/secrets/sqlite/main.db")


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if os.path.exists(schema_path):
        with closing(get_db()) as db, open(schema_path) as f:
            db.executescript(f.read())


init_db()


# ─── Owner Dashboard ───


@app.route("/")
async def index():
    with closing(get_db()) as db:
        secrets = db.execute("SELECT * FROM secrets ORDER BY key").fetchall()
    google_id, google_secret = _get_provider_creds("google")
    return await render_template(
        "index.html", secrets=secrets, show_google_oauth_hint=not (google_id and google_secret)
    )


@app.route("/api/secrets", methods=["GET"])
async def list_secrets():
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description, created_at, updated_at FROM secrets ORDER BY key").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/secrets", methods=["POST"])
async def set_secret():
    data = await request.get_json()
    if not data or not data.get("key") or "value" not in data:
        return jsonify({"error": "key and value are required"}), 400
    with closing(get_db()) as db:
        db.execute(
            """INSERT INTO secrets (key, value, description)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   description = excluded.description,
                   updated_at = datetime('now')""",
            (data["key"], data["value"], data.get("description", "")),
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/secrets/<key>", methods=["DELETE"])
async def delete_secret(key):
    with closing(get_db()) as db:
        db.execute("DELETE FROM secrets WHERE key = ?", (key,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/import", methods=["POST"])
async def import_secrets():
    """Import secrets from a shell-style env file.

    Parses lines like:
        export KEY=value
        export KEY="value"
        export KEY='value'
        KEY=value
    Skips comments (#) and blank lines. Upserts all parsed key-value pairs.
    """
    data = await request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "content is required"}), 400

    parsed = _parse_env_file(data["content"])
    description = data.get("description", "")

    with closing(get_db()) as db:
        imported = 0
        skipped = 0
        for key, value in parsed:
            if not value:
                skipped += 1
                continue
            db.execute(
                """INSERT INTO secrets (key, value, description)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = datetime('now')""",
                (key, value, description),
            )
            imported += 1
        db.commit()
    return jsonify({"ok": True, "imported": imported, "skipped": skipped})


def _parse_env_file(content):
    """Parse shell-style env file. Returns list of (key, value) tuples."""
    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading 'export '
        if line.startswith("export "):
            line = line[7:]
        # Match KEY=VALUE (value may be quoted or empty)
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        # Strip matching quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        results.append((key, value))
    return results


# ─── Service API (called by other apps via router) ───


@app.route("/_service/get", methods=["POST"])
async def service_get():
    """Return secret values for the requested keys.

    The router has already verified that the calling app has permission
    for all requested keys before proxying here.
    """
    data = await request.get_json()
    requested_keys = data.get("keys", []) if data else []

    if not requested_keys:
        return jsonify({"error": "No keys requested"}), 400

    with closing(get_db()) as db:
        result = {}
        for key in requested_keys:
            row = db.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
            if row:
                result[key] = row["value"]

    missing = [k for k in requested_keys if k not in result]
    response = {"secrets": result}
    if missing:
        response["missing"] = missing
        response["message"] = (
            f"The following secrets are not set: {', '.join(missing)}. Add them in the secrets app dashboard."
        )
    return jsonify(response)


@app.route("/_service/list", methods=["GET"])
async def service_list():
    """List available secret keys (names only, not values)."""
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description FROM secrets ORDER BY key").fetchall()
    return jsonify({"keys": [{"key": r["key"], "description": r["description"]} for r in rows]})


APP_NAME = os.environ["OPENHOST_APP_NAME"]
ZONE_DOMAIN = os.environ["OPENHOST_ZONE_DOMAIN"]
MY_REDIRECT_DOMAIN = os.environ["OPENHOST_MY_REDIRECT_DOMAIN"]
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")

DYNAMIC_CRED_PROVIDERS = {"google"}

OAUTH_REDIRECT_URI = f"https://{MY_REDIRECT_DOMAIN}/{APP_NAME}/oauth/callback"


# ─── V2 Service API (permissions validated by provider) ───


def _parse_v2_grants() -> tuple[set[str], bool]:
    """Read X-OpenHost-Permissions header and return (granted_keys, grant_all)."""
    perms_header = request.headers.get("X-OpenHost-Permissions", "[]")
    try:
        grants = json.loads(perms_header)
    except json.JSONDecodeError:
        return set(), False

    granted_keys: set[str] = set()
    for g in grants:
        payload = g.get("grant", {})
        if isinstance(payload, dict):
            if payload.get("key") == "*":
                return set(), True
            if "key" in payload:
                granted_keys.add(payload["key"])
    return granted_keys, False


@app.route("/_service_v2/get", methods=["POST"])
async def service_v2_get():
    """Return secret values for the requested keys (V2: provider-side permission check)."""
    data = await request.get_json()
    requested_keys = data.get("keys", []) if data else []

    if not requested_keys:
        return jsonify({"error": "No keys requested"}), 400

    granted_keys, grant_all = _parse_v2_grants()
    if not grant_all:
        missing_perms = [k for k in requested_keys if k not in granted_keys]
        if missing_perms:
            return jsonify(
                {
                    "error": "permission_required",
                    "required_grant": {
                        "grant_payload": {"key": missing_perms[0]},
                    },
                }
            ), 403

    with closing(get_db()) as db:
        result = {}
        for key in requested_keys:
            row = db.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
            if row:
                result[key] = row["value"]

    missing = [k for k in requested_keys if k not in result]
    response = {"secrets": result}
    if missing:
        response["missing"] = missing
    return jsonify(response)


@app.route("/_service_v2/list", methods=["GET"])
async def service_v2_list():
    """List available secret keys (V2: no permission check needed for names)."""
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description FROM secrets ORDER BY key").fetchall()
    return jsonify({"keys": [{"key": r["key"], "description": r["description"]} for r in rows]})


# ─── OAuth V2 Service API (permissions validated by provider) ───


def _parse_oauth_v2_grants() -> list[tuple[str, str]]:
    """Read X-OpenHost-Permissions header and return granted (provider, scope) pairs."""
    perms_header = request.headers.get("X-OpenHost-Permissions", "[]")
    try:
        grants = json.loads(perms_header)
    except json.JSONDecodeError:
        return []

    result = []
    for g in grants:
        payload = g.get("grant", {})
        if isinstance(payload, dict) and "provider" in payload and "scope" in payload:
            result.append((payload["provider"], payload["scope"]))
    return result


def _check_oauth_v2_permission(provider: str, scopes: list[str]) -> list[dict]:
    """Check if the caller has grants for all requested provider+scope pairs.

    Returns a list of missing grant payloads (empty if all granted).
    """
    granted = set(_parse_oauth_v2_grants())
    missing = []
    for scope in scopes:
        if (provider, scope) not in granted:
            missing.append({"provider": provider, "scope": scope})
    return missing


def _oauth_permission_denied(provider: str, scopes: list[str], missing: list[dict], return_to: str = ""):
    """Build a 403 response with an app-scoped grant_url for missing OAuth permissions."""
    consumer_app = request.headers.get("X-OpenHost-Consumer", "")
    params = urlencode(
        {
            "provider": provider,
            "scopes": ",".join(scopes),
            "consumer": consumer_app,
            "return_to": return_to,
        }
    )
    first = missing[0]
    return jsonify(
        {
            "error": "permission_required",
            "required_grant": {
                "grant_payload": {"provider": first["provider"], "scope": first["scope"]},
                "scope": "app",
                "grant_url": f"//{APP_NAME}.{ZONE_DOMAIN}/oauth/grant?{params}",
            },
        }
    ), 403


@app.route("/_oauth_v2/token", methods=["POST"])
async def oauth_v2_token():
    """V2 OAuth token endpoint — provider-side permission check."""
    data = await request.get_json()
    if not data or not data.get("provider") or not data.get("scopes"):
        return jsonify({"error": "provider and scopes are required"}), 400

    provider = data["provider"]
    scopes = data["scopes"]
    return_to = data.get("return_to", "")

    missing = _check_oauth_v2_permission(provider, scopes)
    if missing:
        return _oauth_permission_denied(provider, scopes, missing, return_to)

    return await service_oauth_token()


@app.route("/_oauth_v2/accounts", methods=["POST"])
async def oauth_v2_accounts():
    """V2 OAuth accounts endpoint — provider-side permission check."""
    data = await request.get_json()
    if not data or not data.get("provider") or not data.get("scopes"):
        return jsonify({"error": "provider and scopes are required"}), 400

    provider = data["provider"]
    scopes = data["scopes"]

    missing = _check_oauth_v2_permission(provider, scopes)
    if missing:
        return _oauth_permission_denied(provider, scopes, missing)

    return await service_oauth_accounts()


# ─── OAuth Service API (shared handlers) ───


def _provider_cred_keys(provider_name: str) -> tuple[str, str]:
    p = provider_name.upper()
    return f"{p}_OAUTH_CLIENT_ID", f"{p}_OAUTH_CLIENT_SECRET"


def _get_provider_creds(provider_name: str) -> tuple[str | None, str | None]:
    """Return (client_id, client_secret) for a provider.

    For providers in DYNAMIC_CRED_PROVIDERS, looks up `<PROVIDER>_OAUTH_CLIENT_ID`
    and `<PROVIDER>_OAUTH_CLIENT_SECRET` in the secrets table. Either may be
    None if not set. For other providers, returns the static values from PROVIDERS.
    """
    if provider_name in DYNAMIC_CRED_PROVIDERS:
        id_key, secret_key = _provider_cred_keys(provider_name)
        with closing(get_db()) as db:
            rows = db.execute(
                "SELECT key, value FROM secrets WHERE key IN (?, ?)",
                (id_key, secret_key),
            ).fetchall()
        m = {r["key"]: r["value"] for r in rows}
        return m.get(id_key), m.get(secret_key)
    p = PROVIDERS[provider_name]
    return p.get("client_id"), p.get("client_secret")


def _credentials_required_response(provider_name: str):
    """Return a 503 JSON response indicating required credential keys are missing."""
    id_key, secret_key = _provider_cred_keys(provider_name)
    return jsonify(
        {
            "error": "credentials_required",
            "message": (
                f"{provider_name.capitalize()} OAuth requires {id_key} and {secret_key} to be set in the secrets app."
            ),
        }
    ), 503


@app.route("/_service/oauth/token", methods=["POST"])
async def service_oauth_token():
    """Return an OAuth access token, or a URL to authorize if none exists.

    The router has already verified that the calling app has permission
    for all requested scopes before proxying here.

    Returns the token if cached/refreshable. Otherwise returns
    status=authorization_required with an authorize_url the app should
    redirect the user to.
    """
    data = await request.get_json()
    if not data or not data.get("provider") or not data.get("scopes"):
        return jsonify({"error": "provider and scopes are required"}), 400

    provider_name = data["provider"]
    scopes = data["scopes"]
    return_to = data.get("return_to", "/")
    account = data.get("account", "default")

    if provider_name not in PROVIDERS:
        return jsonify({"error": "unknown_provider", "provider": provider_name}), 400

    scopes_key = normalize_scopes(scopes)

    # Check for cached token — exact match first, then fall back to the sole
    # token for this provider+scopes when account is "default" (single-account flow).
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ? AND account = ?",
            (provider_name, scopes_key, account),
        ).fetchone()
        if not row and account == "default":
            rows = db.execute(
                "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ?",
                (provider_name, scopes_key),
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]

    if row:
        # Check if token is still valid (with 60s buffer)
        expires_at = row["expires_at"]
        if not expires_at:
            # Token doesn't expire (e.g. GitHub)
            return jsonify(
                {
                    "access_token": row["access_token"],
                    "expires_at": None,
                    "token_type": "Bearer",
                }
            )
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if exp > now + timedelta(seconds=60):
            return jsonify(
                {
                    "access_token": row["access_token"],
                    "expires_at": row["expires_at"],
                    "token_type": "Bearer",
                }
            )

        # Token expired — try refresh
        if row["refresh_token"]:
            client_id, client_secret = _get_provider_creds(provider_name)
            if provider_name in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
                return _credentials_required_response(provider_name)
            refreshed = await refresh_access_token(provider_name, row["refresh_token"], client_id, client_secret)
            if refreshed and "access_token" in refreshed:
                expires_at = None
                if refreshed.get("expires_in"):
                    expires_at = (datetime.now(UTC) + timedelta(seconds=refreshed["expires_in"])).isoformat()
                with closing(get_db()) as db:
                    db.execute(
                        """UPDATE oauth_tokens
                           SET access_token = ?, expires_at = ?, updated_at = datetime('now')
                           WHERE id = ?""",
                        (
                            refreshed["access_token"],
                            expires_at,
                            row["id"],
                        ),
                    )
                    db.commit()
                return jsonify(
                    {
                        "access_token": refreshed["access_token"],
                        "expires_at": expires_at,
                        "token_type": "Bearer",
                    }
                )

    # No valid token — initiate the appropriate flow
    provider = PROVIDERS[provider_name]
    flow_type = provider.get("flow", "auth_code")

    if flow_type == "device":
        if not ZONE_DOMAIN:
            return jsonify({"error": "ZONE_DOMAIN not configured, cannot start device flow"}), 500
        # Device flow: return a URL to our local device authorization page
        params = urlencode(
            {
                "provider": provider_name,
                "scopes": ",".join(scopes),
                "return_to": return_to,
                "account": account,
            }
        )
        # Use the secrets app's subdomain (secrets.<zone_domain>)
        authorize_url = f"//secrets.{ZONE_DOMAIN}/oauth/device?{params}"
    else:
        client_id, client_secret = _get_provider_creds(provider_name)
        if provider_name in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
            return _credentials_required_response(provider_name)
        # Auth code flow: return the provider's consent page URL
        authorize_url = build_auth_url(
            provider_name, scopes, OAUTH_REDIRECT_URI, return_to, client_id, account=account
        )

    return jsonify(
        {
            "status": "authorization_required",
            "authorize_url": authorize_url,
        }
    ), 401


@app.route("/_service/oauth/accounts", methods=["POST"])
async def service_oauth_accounts():
    """List connected accounts for a given provider and scopes.

    Returns account labels that have valid tokens stored.
    """
    data = await request.get_json()
    if not data or not data.get("provider") or not data.get("scopes"):
        return jsonify({"error": "provider and scopes are required"}), 400

    provider_name = data["provider"]
    scopes = data["scopes"]
    scopes_key = normalize_scopes(scopes)

    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT account FROM oauth_tokens WHERE provider = ? AND scopes = ? ORDER BY account",
            (provider_name, scopes_key),
        ).fetchall()

    return jsonify({"accounts": [r["account"] for r in rows]})


# ─── OAuth Grant Flow (app-scoped permission) ───


OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth"


@app.route("/oauth/grant")
async def oauth_grant():
    """Start an OAuth consent flow on behalf of a consumer app.

    After the user authorizes, the callback will store the token, determine the
    account identity, grant an app-scoped permission via the router API, and
    redirect back to the consumer's return_to URL.
    """
    provider_name = request.args.get("provider", "")
    scopes_str = request.args.get("scopes", "")
    consumer = request.args.get("consumer", "")
    return_to = request.args.get("return_to", "")

    if not provider_name or provider_name not in PROVIDERS:
        return Response(f"Unknown provider: {provider_name}", status=400)
    if not scopes_str:
        return Response("No scopes specified", status=400)
    if not consumer:
        return Response("No consumer app specified", status=400)

    scopes = scopes_str.split(",")
    provider = PROVIDERS[provider_name]
    flow_type = provider.get("flow", "auth_code")

    if flow_type == "device":
        params = urlencode(
            {
                "provider": provider_name,
                "scopes": scopes_str,
                "return_to": return_to,
                "account": "default",
                "consumer": consumer,
            }
        )
        return redirect(f"/oauth/device?{params}")

    client_id, client_secret = _get_provider_creds(provider_name)
    if provider_name in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        return _credentials_required_response(provider_name)

    authorize_url = build_auth_url(provider_name, scopes, OAUTH_REDIRECT_URI, return_to, client_id, account="default")
    # Attach consumer info to the pending flow so the callback can grant the permission
    state = list(pending_auth_flows.keys())[-1]
    pending_auth_flows[state]["consumer_app"] = consumer
    pending_auth_flows[state]["service_url"] = OAUTH_SERVICE_URL

    return redirect(authorize_url)


async def _grant_app_scoped_permission(consumer_app: str, service_url: str, grant: dict) -> bool:
    """Call the router API to grant an app-scoped permission for consumer_app."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ROUTER_URL}/api/permissions_v2/grant-app-scoped",
            json={
                "consumer_app": consumer_app,
                "service_url": service_url,
                "grant": grant,
            },
            headers={"Authorization": f"Bearer {APP_TOKEN}"},
        )
        return resp.status_code == 200


# ─── OAuth Callback (auth code flow) ───


@app.route("/oauth/callback")
async def oauth_callback():
    """Handle the OAuth redirect from Google after user authorizes."""
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return Response(f"Authorization denied: {error}", status=400)

    if not code or not state:
        return Response("Missing code or state parameter", status=400)

    flow = pending_auth_flows.pop(state, None)
    if not flow:
        return Response("Invalid or expired authorization flow", status=400)

    # Verify all requested scopes were granted (Google granular consent lets users uncheck scopes)
    granted_scopes_str = request.args.get("scope", "")
    if granted_scopes_str and flow["scopes"]:
        granted = set(granted_scopes_str.split())
        missing = [s for s in flow["scopes"] if s not in granted]
        if missing:
            return Response(
                f"Authorization incomplete — the following permissions were not granted: {', '.join(missing)}. "
                f"Please try again and make sure all permissions are checked.",
                status=400,
            )

    # Exchange code for tokens
    client_id, client_secret = _get_provider_creds(flow["provider"])
    if flow["provider"] in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        id_key, secret_key = _provider_cred_keys(flow["provider"])
        return Response(
            f"OAuth credentials missing: set {id_key} and {secret_key} in the secrets app.",
            status=503,
        )
    result = await exchange_code(flow["provider"], code, flow["redirect_uri"], client_id, client_secret)

    if "error" in result:
        return Response(
            f"Token exchange failed: {result.get('error_description', result['error'])}",
            status=502,
        )

    # Resolve account name from provider identity (e.g. Google email, GitHub username)
    scopes_key = normalize_scopes(flow["scopes"])
    account = flow.get("account", "default")
    identity = await fetch_account_identity(flow["provider"], result["access_token"])
    if identity:
        account = identity

    # Store the token
    with closing(get_db()) as db:
        db.execute(
            """INSERT INTO oauth_tokens (provider, scopes, account, access_token, refresh_token, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, scopes, account) DO UPDATE SET
                   access_token = excluded.access_token,
                   refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
                   expires_at = excluded.expires_at,
                   updated_at = datetime('now')""",
            (
                flow["provider"],
                scopes_key,
                account,
                result["access_token"],
                result.get("refresh_token"),
                result.get("expires_at"),
            ),
        )
        db.commit()

    # If this flow was initiated from /oauth/grant, grant the app-scoped permission
    consumer_app = flow.get("consumer_app")
    if consumer_app:
        for scope in flow["scopes"]:
            await _grant_app_scoped_permission(
                consumer_app,
                flow["service_url"],
                {"provider": flow["provider"], "scope": scope, "account": account},
            )

    # Allow relative paths and protocol-relative URLs to zone subdomains
    return_to = flow["return_to"]
    if not return_to.startswith("/"):
        return_to = "/"
    if return_to.startswith("//"):
        if not ZONE_DOMAIN:
            # Cannot validate domain — reject protocol-relative URLs
            return_to = "/"
        else:
            parts = return_to.split("/")
            # parts[0] and parts[1] are empty strings from "//", parts[2] is the domain
            domain = parts[2] if len(parts) >= 3 else ""
            # Require exact match or subdomain match (with leading dot) to prevent
            # sibling domain spoofing (e.g. "evil.example.com" matching "example.com")
            if domain != ZONE_DOMAIN and not domain.endswith(f".{ZONE_DOMAIN}"):
                return_to = "/"
    return redirect(return_to)


# ─── OAuth Device Flow (GitHub etc.) ───


@app.route("/oauth/device")
async def oauth_device_page():
    """User-facing page: start a device flow and show the code + verification link."""
    provider_name = request.args.get("provider", "")
    scopes_str = request.args.get("scopes", "")
    return_to = request.args.get("return_to", "/")
    account = request.args.get("account", "default")

    if not provider_name or provider_name not in PROVIDERS:
        return Response(f"Unknown provider: {provider_name}", status=400)
    if not scopes_str:
        return Response("No scopes specified", status=400)

    scopes = scopes_str.split(",")

    flow_data = await start_device_flow(provider_name, scopes)
    if "error" in flow_data:
        return Response(
            f"Failed to start device flow: {flow_data.get('error_description', flow_data['error'])}",
            status=502,
        )

    flow_id = create_device_flow(provider_name, scopes, flow_data, account=account)
    flow = active_device_flows[flow_id]

    return await render_template(
        "oauth_device.html",
        user_code=flow["user_code"],
        verification_url=flow["verification_url"],
        flow_id=flow_id,
        provider=provider_name,
        scopes=scopes,
        return_to=return_to,
        zone_domain=ZONE_DOMAIN,
    )


@app.route("/oauth/device/poll/<flow_id>")
async def oauth_device_poll(flow_id):
    """Polled by the device flow page to check if authorization completed."""
    flow = active_device_flows.get(flow_id)
    if not flow:
        return jsonify({"status": "expired", "error": "Flow not found or expired"})

    if flow["status"] == "completed":
        result = flow["result"]
        scopes_key = normalize_scopes(flow["scopes"])
        account = flow.get("account", "default")
        identity = await fetch_account_identity(flow["provider"], result["access_token"])
        if identity:
            account = identity
        with closing(get_db()) as db:
            db.execute(
                """INSERT INTO oauth_tokens (provider, scopes, account, access_token, refresh_token, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, scopes, account) DO UPDATE SET
                       access_token = excluded.access_token,
                       refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
                       expires_at = excluded.expires_at,
                       updated_at = datetime('now')""",
                (
                    flow["provider"],
                    scopes_key,
                    account,
                    result["access_token"],
                    result.get("refresh_token"),
                    result.get("expires_at"),
                ),
            )
            db.commit()
        active_device_flows.pop(flow_id, None)
        return jsonify({"status": "completed"})

    if flow["status"] == "error":
        active_device_flows.pop(flow_id, None)
        return jsonify({"status": "error", "error": flow["result"].get("message", "Unknown error")})

    return jsonify({"status": "pending"})


# ─── OAuth Dashboard APIs ───


@app.route("/api/oauth/status", methods=["GET"])
async def oauth_status():
    """List stored tokens."""
    with closing(get_db()) as db:
        tokens = db.execute(
            "SELECT id, provider, scopes, account, expires_at, created_at, updated_at FROM oauth_tokens ORDER BY provider, account"
        ).fetchall()

    return jsonify(
        {
            "tokens": [dict(r) for r in tokens],
        }
    )


@app.route("/api/oauth/tokens/<int:token_id>", methods=["DELETE"])
async def delete_oauth_token(token_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT provider, access_token, refresh_token FROM oauth_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    if not row:
        return jsonify({"ok": True})

    client_id, client_secret = _get_provider_creds(row["provider"])
    if row["provider"] in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        id_key, secret_key = _provider_cred_keys(row["provider"])
        return jsonify(
            {
                "error": "credentials_required",
                "message": (
                    f"Cannot revoke {row['provider']} token: {id_key} and {secret_key} "
                    f"must be set in the secrets app to contact the provider."
                ),
            }
        ), 503

    # Delete from DB first, then revoke with provider, to avoid holding the DB
    # connection across an external HTTP call.
    with closing(get_db()) as db:
        db.execute("DELETE FROM oauth_tokens WHERE id = ?", (token_id,))
        db.commit()
    token_to_revoke = row["refresh_token"] or row["access_token"]
    await revoke_token(row["provider"], token_to_revoke, client_id, client_secret)
    return jsonify({"ok": True})


# ─── Test Helpers ───


@app.route("/test/set-provider-config", methods=["POST"])
async def test_set_provider_config():
    """Override OAuth provider URLs for testing."""
    global OAUTH_REDIRECT_URI
    data = await request.get_json()
    provider = data.get("provider")
    overrides = data.get("overrides", {})

    if provider and provider in PROVIDERS:
        for key in ("auth_url", "token_url"):
            if key in overrides:
                PROVIDERS[provider][key] = overrides[key]
        if "userinfo_url" in overrides:
            USERINFO_URLS[provider] = (overrides["userinfo_url"], USERINFO_URLS.get(provider, ("", "email"))[1])

    if "redirect_uri" in data:
        OAUTH_REDIRECT_URI = data["redirect_uri"]

    return jsonify({"ok": True})
