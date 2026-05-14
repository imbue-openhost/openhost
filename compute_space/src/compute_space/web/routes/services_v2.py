"""V2 cross-app service proxy: git-URL-based service identity, versioned routing,
provider-side permission validation."""

import json
import sqlite3
from typing import Any
from urllib.parse import urlparse

import attr
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from quart import Blueprint
from quart import Response
from quart import request
from quart import url_for
from quart import websocket

from compute_space.config import get_config
from compute_space.core.apps import find_app_by_name
from compute_space.core.auth.auth import resolve_app_from_token
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.containers import get_docker_logs
from compute_space.core.installer import GRANT_KEY_CAPABILITY
from compute_space.core.installer import GRANT_KEY_REPO_URL_PREFIX
from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import INSTALLER_SERVICE_VERSION
from compute_space.core.installer import INSTALL_CAPABILITY
from compute_space.core.installer import InstallError
from compute_space.core.installer import check_install_allowed
from compute_space.core.installer import install_from_repo_url
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.services_v2 import ShortnameNotDeclared
from compute_space.core.services_v2 import lookup_shortname
from compute_space.core.services_v2 import resolve_provider
from compute_space.db import get_db
from compute_space.web.auth.middleware import _app_from_origin
from compute_space.web.auth.middleware import app_auth_required
from compute_space.web.proxy import proxy_request
from compute_space.web.proxy import ws_proxy

services_v2_bp = Blueprint("services_v2", __name__)


def _cors_origin() -> str | None:
    """Return the Origin header iff it points at a valid app subdomain of this zone."""
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        if not referer:
            return None
        origin = referer

    parsed = urlparse(origin)
    host = parsed.netloc or ""
    raw_origin = f"{parsed.scheme}://{host}" if parsed.scheme else None

    config = get_config()
    if not config.zone_domain or not host.endswith("." + config.zone_domain):
        return None

    app_name = host[: -(len(config.zone_domain) + 1)]
    if "." in app_name:
        return None

    return raw_origin


def _add_cors_headers(response: Response, origin: str) -> Response:
    """Add CORS headers for cross-origin requests from app subdomains."""
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ─── CORS ───


@services_v2_bp.after_request
async def add_cors_to_services_v2(response: Response) -> Response:
    if request.path.startswith("/api/services/v2/call/"):
        origin = _cors_origin()
        if origin:
            _add_cors_headers(response, origin)
    return response


@services_v2_bp.route("/api/services/v2/call/<shortname>/<path:rest>", methods=["OPTIONS"])
async def service_call_cors(shortname: str, rest: str) -> Response:
    origin = _cors_origin()
    if not origin:
        return Response("Forbidden", status=403)
    return _add_cors_headers(Response("", status=204), origin)


async def _add_grant_url_if_global_grant_needed(
    response: Response,
    service_url: str,
    consumer_app_id: str,
) -> Response:
    """Add grant_url to service provider 403 responses that indicate a missing globally-scoped permission grant.

    Providers return 403 when the consumer lacks a required permission. Expected format:
        Global:  {"error": "permission_required", "required_grant": { "grant": ..., "scope": "global" }}
        App:     {"error": "permission_required", "required_grant": { "grant": ...,
                     "scope": "app", "grant_url": "https://..." }}

    For global grants, this fn adds a grant_url that points to the owner-facing approval page for the required grant.
    For app-scoped grants, the provider is expected to include a grant_url in its response.

    If the 403 body doesn't contain `required_grant`, the response is passed through unchanged.
    """
    try:
        body = json.loads(await response.get_data())
    except (json.JSONDecodeError, Exception):
        return response

    required_grant = body.get("required_grant")
    if not isinstance(required_grant, dict):
        return response

    if required_grant.get("scope", "global") != "global":
        return response

    grant = required_grant.get("grant")
    if not isinstance(grant, (str, dict)):
        # we can't make a grant URL without a valid grant payload, so just return the original response even if it's malformed
        return response

    config = get_config()
    approve_path = url_for(
        "pages_permissions_v2.approve_permissions_v2",
        app=consumer_app_id,
        service=service_url,
        grant=json.dumps(grant, sort_keys=True),
    )
    required_grant["grant_url"] = f"https://{config.zone_domain}{approve_path}"

    return Response(
        json.dumps(body),
        status=403,
        content_type="application/json",
    )


def _json_error(error: str, message: str, status: int) -> Response:
    return Response(
        json.dumps({"error": error, "message": message}),
        status=status,
        content_type="application/json",
    )


@services_v2_bp.route("/api/services/v2/oauth_callback")
async def oauth_callback_proxy_v2() -> Response:
    """Proxy OAuth provider callbacks to the correct oauth service app.

    OAuth providers (Google, GitHub, etc.) redirect to a fixed callback URL on MY_REDIRECT_DOMAIN after user
    authorization. This endpoint receives that redirect and forwards it to the oauth app that initiated the flow.

    The oauth app encodes its app name in the OAuth ``state`` parameter as JSON:
    ``{"app": "<app_name>", "nonce": "<random>"}``. This endpoint parses that to determine which app should receive
    the callback, then proxies the full request (including code, state, scope query params) to that app's
    ``/callback`` handler.
    """
    state_raw = request.args.get("state", "")
    if not state_raw:
        return _json_error("bad_request", "Missing state parameter", 400)

    try:
        state = json.loads(state_raw)
    except json.JSONDecodeError:
        return _json_error("bad_request", "Invalid state parameter", 400)

    app_name = state.get("app")
    if not app_name or not isinstance(app_name, str):
        return _json_error("bad_request", "Missing app in state", 400)

    app_row = find_app_by_name(app_name)
    if not app_row:
        return _json_error("service_not_available", f"App '{app_name}' not found", 503)
    if app_row["status"] != "running":
        return _json_error("service_not_available", f"App '{app_name}' is not running", 503)

    return await proxy_request(request, app_row["local_port"], override_path="/callback")


# ─── Shortname-based call proxy ───
#
# Apps declare services they consume in [[services.v2.consumes]] with a shortname; clients call
# /api/services/v2/call/<shortname>/<path> instead of constructing the full service URL +
# headers themselves. The router identifies the consumer (Bearer for server-side, Origin for
# browser) and looks up the shortname in that consumer's stored manifest.


def _resolve_consumer_from_ws() -> str | None:
    """WebSocket counterpart of app_auth_required: Bearer token first, then Origin + JWT cookie."""
    auth_header = websocket.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
    return _app_from_origin(websocket)


def _consumer_identity_headers(consumer_app_id: str) -> dict[str, str]:
    """Build the X-OpenHost-Consumer-{Name,Id} headers for a consumer app.

    Provider apps get both: the human-readable name (good for logs/UI) and the
    stable app_id (good for keying stored data that should survive renames).
    """
    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    name = row["name"] if row else consumer_app_id
    return {
        "X-OpenHost-Consumer-Name": name,
        "X-OpenHost-Consumer-Id": consumer_app_id,
    }


@services_v2_bp.route(
    "/api/services/v2/call/<shortname>/<path:rest>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
@app_auth_required
async def service_call(shortname: str, rest: str, app_id: str) -> Response:
    """Proxy a request to the provider declared under <shortname> in the consumer's manifest.

    Resolution flow:
      1. Identify consumer from Bearer token or Origin subdomain (handled by @app_auth_required).
      2. Load consumer's manifest and find the [[services.v2.consumes]] entry where shortname matches.
      3. Resolve the provider for that service URL + version specifier.
      4. Proxy to <provider_endpoint>/<rest>, injecting X-OpenHost-Permissions and
         X-OpenHost-Consumer-{Name,Id}.
    """
    consumer_app_id = app_id
    db = get_db()

    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        return _json_error("shortname_not_declared", e.message, 404)

    # Installer is a router-internal pseudo-service: it has no provider
    # app and its handlers run in-process to share the router's DB.
    if service_url == INSTALLER_SERVICE_URL:
        return await _handle_installer_request(consumer_app_id, version_spec, rest, db)

    try:
        _, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        return _json_error("service_not_available", e.message, 503)

    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    grants_json = json.dumps([attr.asdict(g) for g in grants])

    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")

    response = await proxy_request(
        request,
        provider_port,
        override_path=target_path,
        extra_headers={
            "Authorization": None,
            "X-OpenHost-Permissions": grants_json,
            **_consumer_identity_headers(consumer_app_id),
        },
    )

    if response.status_code == 403:
        return await _add_grant_url_if_global_grant_needed(response, service_url, consumer_app_id)
    return response


@services_v2_bp.websocket("/api/services/v2/call/<shortname>/<path:rest>")
async def service_call_ws(shortname: str, rest: str) -> None:
    """WebSocket variant of service_call. Same auth + resolution; lifts the request/response proxy to ws_proxy."""
    consumer_app_id = _resolve_consumer_from_ws()
    if not consumer_app_id:
        await websocket.close(code=4401, reason="Missing or invalid authorization")
        return

    db = get_db()
    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        await websocket.close(code=4404, reason=e.message)
        return

    try:
        _, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        await websocket.close(code=4503, reason=e.message)
        return

    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    grants_json = json.dumps([attr.asdict(g) for g in grants])

    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")
    await ws_proxy(
        provider_port,
        client_ws=websocket,
        identity_headers={
            "X-OpenHost-Permissions": grants_json,
            **_consumer_identity_headers(consumer_app_id),
        },
        override_path=target_path,
    )


# ─── Installer (router-internal v2 service) ───
#
# The installer has no provider app — its handlers run in-process so they
# can share the router's DB and apps.* state.  Apps that consume it
# declare:
#
#     [[services.v2.consumes]]
#     service   = "github.com/imbue-openhost/openhost/services/installer"
#     shortname = "installer"
#     version   = ">=0.1.0"
#     grants    = [{capability = "install", repo_url_prefix = "https://..."}]
#
# and call /api/services/v2/call/installer/{install,status/<name>,logs/<name>}.


async def _handle_installer_request(
    consumer_app_id: str, version_spec: str, rest: str, db: sqlite3.Connection
) -> Response:
    """Dispatch ``installer`` v2 service requests in-process.

    Routes:
        POST /install                — body: {repo_url, app_name?}
        GET  /status/<app_name>      — only for apps this consumer installed
        GET  /logs/<app_name>        — only for apps this consumer installed
    """
    try:
        spec = SpecifierSet(version_spec)
    except InvalidSpecifier:
        return _json_error("bad_request", f"Invalid version specifier: {version_spec}", 400)
    if Version(INSTALLER_SERVICE_VERSION) not in spec:
        return _json_error(
            "service_not_available",
            f"installer version {INSTALLER_SERVICE_VERSION} does not match {version_spec}",
            503,
        )

    parts = rest.strip("/").split("/")

    # POST /install
    if request.method == "POST" and parts == ["install"]:
        body = await _read_json_body()
        if not isinstance(body, dict):
            return _json_error("bad_request", "request body must be JSON object", 400)
        repo_url = (body.get("repo_url") or "").strip()
        if not repo_url:
            return _json_error("bad_request", "repo_url is required", 400)
        app_name = (body.get("app_name") or "").strip() or None

        grants = [g.grant for g in get_granted_permissions_v2(consumer_app_id, INSTALLER_SERVICE_URL)]
        if (reason := check_install_allowed(repo_url, grants)) is not None:
            return _installer_permission_denied(consumer_app_id, repo_url, reason, db)

        try:
            result = await install_from_repo_url(
                repo_url, get_config(), db, app_name=app_name, installed_by=consumer_app_id
            )
        except InstallError as exc:
            return _json_error("install_failed", exc.message, exc.status_code)
        return _json_ok({"ok": True, "app_name": result.app_name, "status": result.status})

    # GET /status/<app_name> and GET /logs/<app_name>
    if request.method == "GET" and len(parts) == 2 and parts[0] in ("status", "logs"):
        sub, app_name = parts
        row, denied = _lookup_consumer_install(consumer_app_id, app_name, db)
        if denied is not None:
            return denied
        assert row is not None
        if sub == "status":
            return _json_ok({"status": row["status"], "error": row["error_message"]})
        logs = get_docker_logs(app_name, get_config().temporary_data_dir, row["container_id"])
        return Response(logs, status=200, content_type="text/plain; charset=utf-8")

    return _json_error("bad_request", f"Unknown installer endpoint: {request.method} /{rest}", 404)


async def _read_json_body() -> Any:
    try:
        return await request.get_json()
    except Exception:
        return None


def _lookup_consumer_install(
    consumer_app_id: str, app_name: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response | None]:
    """Fetch the apps row only if ``consumer_app_id`` installed it.

    Returns ``(row, None)`` on success or ``(None, error_response)`` on
    not_found / forbidden.  The row carries every column callers need so
    /status and /logs can share the lookup.
    """
    row = db.execute(
        "SELECT status, error_message, container_id, installed_by FROM apps WHERE name = ?",
        (app_name,),
    ).fetchone()
    if not row:
        return None, _json_error("not_found", f"app {app_name!r} not found", 404)
    if row["installed_by"] != consumer_app_id:
        return None, _json_error("forbidden", f"{consumer_app_id} did not install {app_name!r}", 403)
    return row, None


def _json_ok(body: dict[str, Any]) -> Response:
    return Response(json.dumps(body), status=200, content_type="application/json")


def _proposed_install_grant_from_manifest(
    consumer_app_id: str, repo_url: str, db: sqlite3.Connection
) -> dict[str, str]:
    """Pick the install grant payload to offer the owner on a 403.

    Prefers a grant the consumer **already declared** in its
    ``[[services.v2.consumes]]`` block for the installer service whose
    ``repo_url_prefix`` matches ``repo_url`` — so a manifest-declared
    broad grant (e.g. ``"https://github.com/"``) gets approved once and
    covers every subsequent install instead of producing one approval
    prompt per repo.

    Falls back to a per-URL grant only if the consumer's manifest
    declares no installer grants at all, or none whose prefix covers the
    requested URL.  This keeps behaviour sane for consumers that haven't
    been updated to declare a broader prefix yet.
    """
    fallback = {GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: repo_url}
    row = db.execute("SELECT manifest_raw FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    if not row or not row["manifest_raw"]:
        return fallback
    try:
        manifest = parse_manifest_from_string(row["manifest_raw"])
    except Exception:
        return fallback

    for consume in manifest.consumes_services_v2:
        if consume.service != INSTALLER_SERVICE_URL:
            continue
        for g in consume.grants:
            if not isinstance(g, dict):
                continue
            if g.get(GRANT_KEY_CAPABILITY) != INSTALL_CAPABILITY:
                continue
            prefix = g.get(GRANT_KEY_REPO_URL_PREFIX, "")
            if not isinstance(prefix, str):
                continue
            if prefix in ("", "*") or repo_url.startswith(prefix):
                return {
                    GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY,
                    GRANT_KEY_REPO_URL_PREFIX: prefix,
                }
    return fallback


def _installer_permission_denied(consumer_app_id: str, repo_url: str, reason: str, db: sqlite3.Connection) -> Response:
    """Return the v2 standard permission_required shape with a grant URL.

    The proposed grant comes from the consumer's manifest-declared
    ``[[services.v2.consumes]]`` block when possible, so approving once
    covers every repo the consumer's manifest already declared a prefix
    for.  Falls back to a per-URL grant only if the manifest declares
    no matching prefix.
    """
    grant = _proposed_install_grant_from_manifest(consumer_app_id, repo_url, db)
    try:
        approve_path = url_for(
            "pages_permissions_v2.approve_permissions_v2",
            app=consumer_app_id,
            service=INSTALLER_SERVICE_URL,
            grant=json.dumps(grant, sort_keys=True),
        )
        grant_url = f"https://{get_config().zone_domain}{approve_path}"
    except Exception:
        # In tests where the permissions blueprint isn't registered.
        grant_url = ""
    body = {
        "error": "permission_required",
        "message": reason,
        "required_grant": {"grant": grant, "scope": "global", "grant_url": grant_url},
    }
    return Response(json.dumps(body), status=403, content_type="application/json")
