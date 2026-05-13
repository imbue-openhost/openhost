"""V2 cross-app service proxy: git-URL-based service identity, versioned routing, provider-side permission validation."""

import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

import attr
from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar import get
from litestar import route
from litestar import websocket
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space.config import get_config
from compute_space.core.apps import find_app_by_name
from compute_space.core.auth import resolve_app_from_token
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
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services_v2 import ShortnameNotDeclared
from compute_space.core.services_v2 import lookup_shortname
from compute_space.core.services_v2 import resolve_provider
from compute_space.db import get_db
from compute_space.web.auth.middleware import _app_from_origin
from compute_space.web.proxy import proxied_to_litestar
from compute_space.web.proxy import proxy_request_raw
from compute_space.web.proxy import ws_proxy
from compute_space.web.routes.services import _add_cors_headers
from compute_space.web.routes.services import _cors_origin


def _add_cors_v2(request: Request[Any, Any, Any], response: Response[Any]) -> Response[Any]:
    if request.url.path.startswith("/api/services/v2/call/"):
        origin = _cors_origin(request)
        if origin:
            _add_cors_headers(response, origin)
    return response


@route("/api/services/v2/call/{shortname:str}/{rest:path}", http_method=["OPTIONS"])
async def service_call_cors(request: Request[Any, Any, Any], shortname: str, rest: str) -> Response[bytes]:
    origin = _cors_origin(request)
    if not origin:
        return Response(content=b"Forbidden", status_code=403, media_type="text/plain")
    response: Response[bytes] = Response(content=b"", status_code=204)
    _add_cors_headers(response, origin)
    return response


def _json_error(error: str, message: str, status: int) -> Response[dict[str, Any]]:
    return Response(content={"error": error, "message": message}, status_code=status)


def _json_ok(body: dict[str, Any]) -> Response[dict[str, Any]]:
    return Response(content=body)


async def _maybe_add_grant_url_to_global_grant(
    response: Response[Any], service_url: str, consumer_app_id: str
) -> Response[Any]:
    body_obj: Any
    if isinstance(response.content, (bytes, bytearray)):
        try:
            body_obj = json.loads(bytes(response.content).decode())
        except Exception:
            return response
    elif isinstance(response.content, dict):
        body_obj = response.content
    else:
        return response

    required_grant = body_obj.get("required_grant")
    if not isinstance(required_grant, dict):
        return response
    if required_grant.get("scope", "global") != "global":
        return response
    grant = required_grant.get("grant")
    if not isinstance(grant, (str, dict)):
        return response

    config = get_config()
    qs = urlencode({"app": consumer_app_id, "service": service_url, "grant": json.dumps(grant, sort_keys=True)})
    required_grant["grant_url"] = f"https://{config.zone_domain}/approve-permissions-v2?{qs}"
    return Response(content=body_obj, status_code=403)


@get("/api/services/v2/oauth_callback")
async def oauth_callback_proxy_v2(request: Request[Any, Any, Any]) -> Response[Any]:
    state_raw = request.query_params.get("state", "")
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

    proxied = await proxy_request_raw(
        request.scope,
        request.receive,
        app_row["local_port"],
        override_path="/callback",
    )
    return proxied_to_litestar(proxied)


def _resolve_consumer_from_ws(ws: WebSocket[Any, Any, Any]) -> str | None:
    auth_header = ws.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return resolve_app_from_token(auth_header.removeprefix("Bearer ").strip())
    return _app_from_origin(ws)


def _consumer_identity_headers(consumer_app_id: str) -> dict[str, str]:
    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    name = row["name"] if row else consumer_app_id
    return {
        "X-OpenHost-Consumer-Name": name,
        "X-OpenHost-Consumer-Id": consumer_app_id,
    }


@route(
    "/api/services/v2/call/{shortname:str}/{rest:path}",
    http_method=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
    status_code=200,
)
async def service_call(
    request: Request[Any, Any, Any], shortname: str, rest: str, caller_app_id: str
) -> Response[Any]:
    consumer_app_id = caller_app_id
    rest = rest.lstrip("/")
    db = get_db()

    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        return _add_cors_v2(request, _json_error("shortname_not_declared", e.message, 404))

    if service_url == INSTALLER_SERVICE_URL:
        response = await _handle_installer_request(request, consumer_app_id, version_spec, rest, db)
        return _add_cors_v2(request, response)

    try:
        _, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        return _add_cors_v2(request, _json_error("service_not_available", e.message, 503))

    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    grants_json = json.dumps([attr.asdict(g) for g in grants])

    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")

    proxied = await proxy_request_raw(
        request.scope,
        request.receive,
        provider_port,
        override_path=target_path,
        extra_headers={
            "Authorization": None,
            "X-OpenHost-Permissions": grants_json,
            **_consumer_identity_headers(consumer_app_id),
        },
    )
    response = proxied_to_litestar(proxied)
    if response.status_code == 403:
        response = await _maybe_add_grant_url_to_global_grant(response, service_url, consumer_app_id)
    return _add_cors_v2(request, response)


@websocket("/api/services/v2/call/{shortname:str}/{rest:path}")
async def service_call_ws(socket: WebSocket[Any, Any, Any], shortname: str, rest: str) -> None:
    consumer_app_id = _resolve_consumer_from_ws(socket)
    if not consumer_app_id:
        await socket.close(code=4401, reason="Missing or invalid authorization")
        return

    db = get_db()
    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        await socket.close(code=4404, reason=e.message)
        return

    try:
        _, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        await socket.close(code=4503, reason=e.message)
        return

    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    grants_json = json.dumps([attr.asdict(g) for g in grants])

    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")
    await ws_proxy(
        provider_port,
        client_ws=socket,
        identity_headers={
            "X-OpenHost-Permissions": grants_json,
            **_consumer_identity_headers(consumer_app_id),
        },
        override_path=target_path,
    )


async def _handle_installer_request(
    request: Request[Any, Any, Any], consumer_app_id: str, version_spec: str, rest: str, db: sqlite3.Connection
) -> Response[Any]:
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
    method = request.method

    if method == "POST" and parts == ["install"]:
        try:
            body = await request.json()
        except Exception:
            body = None
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

    if method == "GET" and len(parts) == 2 and parts[0] in ("status", "logs"):
        sub, app_name = parts
        row, denied = _lookup_consumer_install(consumer_app_id, app_name, db)
        if denied is not None:
            return denied
        assert row is not None
        if sub == "status":
            return _json_ok({"status": row["status"], "error": row["error_message"]})
        logs = get_docker_logs(app_name, get_config().temporary_data_dir, row["container_id"])
        return Response(content=logs.encode(), status_code=200, media_type="text/plain; charset=utf-8")

    return _json_error("bad_request", f"Unknown installer endpoint: {method} /{rest}", 404)


def _lookup_consumer_install(
    consumer_app_id: str, app_name: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response[Any] | None]:
    row = db.execute(
        "SELECT status, error_message, container_id, installed_by FROM apps WHERE name = ?",
        (app_name,),
    ).fetchone()
    if not row:
        return None, _json_error("not_found", f"app {app_name!r} not found", 404)
    if row["installed_by"] != consumer_app_id:
        return None, _json_error("forbidden", f"{consumer_app_id} did not install {app_name!r}", 403)
    return row, None


def _proposed_install_grant_from_manifest(
    consumer_app_id: str, repo_url: str, db: sqlite3.Connection
) -> dict[str, str]:
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


def _installer_permission_denied(
    consumer_app_id: str, repo_url: str, reason: str, db: sqlite3.Connection
) -> Response[Any]:
    grant = _proposed_install_grant_from_manifest(consumer_app_id, repo_url, db)
    try:
        config = get_config()
        qs = urlencode(
            {"app": consumer_app_id, "service": INSTALLER_SERVICE_URL, "grant": json.dumps(grant, sort_keys=True)}
        )
        grant_url = f"https://{config.zone_domain}/approve-permissions-v2?{qs}"
    except Exception:
        grant_url = ""
    body = {
        "error": "permission_required",
        "message": reason,
        "required_grant": {"grant": grant, "scope": "global", "grant_url": grant_url},
    }
    return Response(content=body, status_code=403)


services_v2_routes = [service_call_cors, oauth_callback_proxy_v2, service_call, service_call_ws]
