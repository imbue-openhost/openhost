import base64
import json
import sqlite3
import urllib.parse

from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from quart import Blueprint
from quart import Response
from quart import g
from quart import jsonify
from quart import redirect
from quart import render_template
from quart import request
from quart import websocket
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core import auth
from compute_space.core import identity
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.middleware import _try_refresh
from compute_space.web.middleware import login_required
from compute_space.web.proxy import proxy_request
from compute_space.web.proxy import ws_proxy

proxy_bp = Blueprint("proxy", __name__)


def _parse_app_from_host() -> str | None:
    """Extract app name from the Host header using zone_domain config.

    ha-tunnel.zplizzi.host.imbue.com -> "ha-tunnel"
    zplizzi.host.imbue.com -> None
    localhost:8080 -> None
    """
    config = get_config()
    if not config.zone_domain:
        return None
    host = request.host.split(":")[0]
    zone_domain = config.zone_domain
    if host == zone_domain:
        return None
    if host.endswith("." + zone_domain):
        app_name = host[: -(len(zone_domain) + 1)]
        if "." not in app_name:
            return app_name
    return None


def _parse_app_from_host_ws() -> str | None:
    """Extract app name from websocket Host header."""
    config = get_config()
    if not config.zone_domain:
        return None
    host = websocket.host.split(":")[0] if hasattr(websocket, "host") else ""
    zone_domain = config.zone_domain
    if host == zone_domain:
        return None
    if host.endswith("." + zone_domain):
        app_name = host[: -(len(zone_domain) + 1)]
        if "." not in app_name:
            return app_name
    return None


# ─── JWKS endpoint ───


@proxy_bp.route("/.well-known/jwks.json")
def jwks() -> Response:
    """Expose the public key in JWKS format for app JWT verification."""
    public_key_pem = auth.get_public_key_pem()
    public_key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(public_key, rsa_module.RSAPublicKey)
    numbers = public_key.public_numbers()

    def _b64url(num: int, length: int) -> str:
        b = num.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    n_bytes = (numbers.n.bit_length() + 7) // 8

    jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": _b64url(numbers.n, n_bytes),
        "e": _b64url(numbers.e, 3),
    }
    return jsonify({"keys": [jwk]})


# ─── Federated Identity ───


@proxy_bp.route("/.well-known/openhost-identity")
def openhost_identity() -> Response:
    """Public endpoint: expose this zone's identity (domain + public key)."""
    try:
        return jsonify(identity.get_zone_identity())
    except RuntimeError:
        return Response("Identity not yet available", status=503)


@proxy_bp.route("/identity/approve")
@login_required
async def identity_approve() -> ResponseReturnValue:
    """Show the owner an approval page for a federated login request."""
    callback = request.args.get("callback", "").strip()
    app_name = request.args.get("app_name", "an app")
    requesting_domain = request.args.get("requesting_domain", "unknown")

    if not callback:
        return Response("Missing callback parameter", status=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response("Invalid callback URL", status=400)

    return await render_template(
        "identity_approve.html",
        callback=callback,
        app_name=app_name,
        requesting_domain=requesting_domain,
    )


@proxy_bp.route("/identity/approve", methods=["POST"])
@login_required
async def identity_approve_submit() -> ResponseReturnValue:
    """Owner approved the login — sign an identity token and redirect back."""
    form = await request.form
    callback = form.get("callback", "").strip()
    if not callback:
        return Response("Missing callback parameter", status=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response("Invalid callback URL", status=400)

    try:
        token = identity.sign_identity_token(callback)
    except RuntimeError as e:
        logger.error("Failed to sign identity token: %s", e)
        return Response("Identity service unavailable", status=503)

    separator = "&" if "?" in callback else "?"
    encoded_token = urllib.parse.quote(token, safe="")
    return redirect(f"{callback}{separator}identity_token={encoded_token}")


# ─── Reverse Proxy (catch-all) ───


@proxy_bp.route(
    "/",
    defaults={"path": ""},
    methods=[
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "HEAD",
        "OPTIONS",
    ],
)
@proxy_bp.route(
    "/<path:path>",
    methods=[
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "HEAD",
        "OPTIONS",
    ],
)
async def catch_all(path: str) -> ResponseReturnValue:
    """Match request to an app and proxy via subdomain (Host-based) routing."""
    request_path = f"/{path}"
    db = get_db()

    app_subdomain = _parse_app_from_host()
    if app_subdomain:
        app_row = db.execute(
            "SELECT name, local_port, status, public_paths FROM apps WHERE name = ?",
            (app_subdomain,),
        ).fetchone()
        if not app_row:
            return Response(f"App '{app_subdomain}' not found", status=404)
        return await _proxy_to_app(app_row, request_path, base_path="")

    segments = path.split("/", 1)
    if segments and segments[0]:
        app_name = segments[0]
        app_row = db.execute(
            "SELECT name, local_port, status, public_paths FROM apps WHERE name = ?",
            (app_name,),
        ).fetchone()
        if app_row:
            return await _proxy_to_app(app_row, request_path, base_path=f"/{app_name}")

    return "Not found", 404


async def _proxy_to_app(app_row: sqlite3.Row, request_path: str, base_path: str) -> Response:
    """Auth check + proxy request to an app."""
    new_access_token = None
    claims = auth.get_current_user_from_request(request)
    if claims is None:
        claims = _try_refresh()
        if claims:
            new_access_token = getattr(g, "new_access_token", None)

    if claims is None:
        rel_path = request_path[len(base_path) :] if base_path else request_path
        public_paths = json.loads(app_row["public_paths"] or "[]")
        is_public = any(rel_path == pp or rel_path.startswith(pp.rstrip("/") + "/") for pp in public_paths)
        if not is_public:
            # Absolute URL to zone root so subdomain app requests don't loop.
            proto = request.headers.get("X-Forwarded-Proto", request.scheme)
            return redirect(f"{proto}://{get_config().zone_domain}/login")  # type: ignore[return-value]

    identity_headers = {
        "X-OpenHost-Is-Owner": "true" if claims and claims.get("sub") == "owner" else "",
    }

    # Use a longer timeout for large requests (e.g. migration data transfers)
    content_length = request.content_length or 0
    timeout = 600 if content_length > 10 * 1024 * 1024 else 30

    response = await proxy_request(
        request,
        app_row["local_port"],
        base_path,
        extra_headers=identity_headers,  # type: ignore[arg-type]
        timeout=timeout,
    )

    if new_access_token:
        auth.set_auth_cookies(
            response,
            new_access_token,
            request.cookies.get(auth.COOKIE_REFRESH),
            request=request,
        )

    return response


# ─── WebSocket Reverse Proxy (catch-all) ───


@proxy_bp.websocket("/", defaults={"path": ""})
@proxy_bp.websocket("/<path:path>")
async def ws_catch_all(path: str) -> None:
    """Match request to an app and proxy WebSocket."""
    request_path = f"/{path}"
    db = get_db()

    app_name = _parse_app_from_host_ws()
    if app_name:
        app_row = db.execute(
            "SELECT name, local_port, status, public_paths FROM apps WHERE name = ?",
            (app_name,),
        ).fetchone()
        if app_row and app_row["status"] in ("running", "starting"):
            await _ws_proxy_to_app(app_row, request_path, base_path="")
            return
        return

    segments = path.split("/", 1)
    if segments and segments[0]:
        app_name = segments[0]
        app_row = db.execute(
            "SELECT name, local_port, status, public_paths FROM apps WHERE name = ?",
            (app_name,),
        ).fetchone()
        if app_row and app_row["status"] in ("running", "starting"):
            await _ws_proxy_to_app(app_row, request_path, base_path=f"/{app_name}")
            return


async def _ws_proxy_to_app(app_row: sqlite3.Row, request_path: str, base_path: str) -> None:
    """Auth check + proxy WebSocket to an app."""
    claims = auth.get_current_user_from_request(websocket)  # type: ignore[arg-type]
    if claims is None:
        rel_path = request_path[len(base_path) :] if base_path else request_path
        public_paths = json.loads(app_row["public_paths"] or "[]")
        is_public = any(rel_path == pp or rel_path.startswith(pp.rstrip("/") + "/") for pp in public_paths)
        if not is_public:
            return

    identity_headers = {
        "X-OpenHost-Is-Owner": "true" if claims and claims.get("sub") == "owner" else "",
    }
    await ws_proxy(app_row["local_port"], base_path, websocket, identity_headers=identity_headers)
