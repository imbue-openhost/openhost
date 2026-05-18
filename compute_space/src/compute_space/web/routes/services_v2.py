"""V2 cross-app service proxy: git-URL-based service identity, versioned routing,
provider-side permission validation.

Routes:
    OPTIONS /api/services/v2/call/{shortname}/{rest:path} — CORS preflight
    *       /api/services/v2/call/{shortname}/{rest:path} — proxied call
    WS      /api/services/v2/call/{shortname}/{rest:path} — proxied WS call
    GET     /api/services/v2/oauth_callback              — OAuth callback fan-out

The HTTP call route streams the response back to the client by default; on
``403`` the body is buffered (see ``proxy_request``'s ``buffer_status_codes``)
so we can inject ``grant_url`` into the ``permission_required`` payload before
relaying it.
"""

import json
import sqlite3
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from litestar import HttpMethod
from litestar import MediaType
from litestar import Request
from litestar import Router
from litestar import WebSocket
from litestar import get
from litestar import route
from litestar import websocket
from litestar.exceptions import NotAuthorizedException
from litestar.response import Response
from litestar.response.base import ASGIResponse
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.apps import find_app_by_name
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
from compute_space.web.auth.guards import require_app_auth
from compute_space.web.auth.guards import resolve_caller_app_id
from compute_space.web.middleware.subdomain_proxy import proxy_request
from compute_space.web.middleware.subdomain_proxy import ws_proxy

_CALL_PATH = "/api/services/v2/call/{shortname:str}/{rest:path}"
_HTTP_METHODS = [
    HttpMethod.GET,
    HttpMethod.POST,
    HttpMethod.PUT,
    HttpMethod.DELETE,
    HttpMethod.PATCH,
    HttpMethod.HEAD,
]


# ─── Helpers: CORS, JSON shapes ─────────────────────────────────────────────


def _cors_origin(request: Request[Any, Any, Any]) -> str | None:
    """Return the request Origin iff it points at a valid app subdomain of this zone."""
    origin = request.headers.get("Origin", "") or request.headers.get("Referer", "")
    if not origin:
        return None

    parsed = urlparse(origin)
    host = parsed.netloc or ""
    if not parsed.scheme or not host:
        return None

    config = get_config()
    if not config.zone_domain or not host.endswith("." + config.zone_domain):
        return None
    app_name = host[: -(len(config.zone_domain) + 1)]
    if "." in app_name:
        return None

    return f"{parsed.scheme}://{host}"


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def _json_error(error: str, message: str, status: int) -> Response[dict[str, Any]]:
    return Response(content={"error": error, "message": message}, status_code=status, media_type=MediaType.JSON)


def _json_ok(body: dict[str, Any]) -> Response[dict[str, Any]]:
    return Response(content=body, status_code=200, media_type=MediaType.JSON)


def _asgi_json_error(error: str, message: str, status: int) -> ASGIResponse:
    """JSON error as an already-encoded ASGIResponse — for handlers that return ASGIResponse."""
    return ASGIResponse(
        body=json.dumps({"error": error, "message": message}).encode(),
        status_code=status,
        media_type=MediaType.JSON,
    )


# ─── Consumer identity (Bearer-or-cookie-on-subdomain) ──────────────────────


def _consumer_app_id_or_raise(request: Request[Any, Any, Any]) -> str:
    """Read the caller's app_id from scope state.  ``require_app_auth`` runs
    first and guarantees this is set for HTTP requests — the raise is just a
    local invariant."""
    app_id = resolve_caller_app_id(request)
    if app_id is None:
        raise NotAuthorizedException(detail="Missing or invalid app authorization")
    return app_id


# ─── Headers forwarded to the provider ──────────────────────────────────────


def _build_permissions_header(consumer_app_id: str, service_url: str, provider_app_id: str) -> str:
    """JSON for ``X-OpenHost-Permissions`` — the consumer's grants applicable to this provider.

    Includes global-scoped grants and any app-scoped grants targeting this
    provider.  ``provider_app_id`` is stripped from each entry since the
    provider already knows it's the addressee.
    """
    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    forwarded = [
        {"grant": g.grant, "scope": g.scope}
        for g in grants
        if g.scope == "global" or g.provider_app_id == provider_app_id
    ]
    return json.dumps(forwarded)


def _consumer_identity_headers(consumer_app_id: str, db: sqlite3.Connection) -> dict[str, str]:
    """X-OpenHost-Consumer-Name + X-OpenHost-Consumer-Id headers for a consumer.

    Providers get both: the human-readable name (good for logs/UI) and the
    stable app_id (good for keying stored data that should survive renames).
    """
    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    name = row["name"] if row else consumer_app_id
    return {"X-OpenHost-Consumer-Name": name, "X-OpenHost-Consumer-Id": consumer_app_id}


# ─── 403 grant-URL injection ────────────────────────────────────────────────


def _inject_grant_url_if_global(
    response: ASGIResponse,
    service_url: str,
    consumer_app_id: str,
    config: Config,
) -> ASGIResponse:
    """If the provider's 403 body is ``permission_required`` with a global-scoped
    grant request, decorate it with ``grant_url`` pointing at the owner-facing
    approval page.  The provider populates ``grant_url`` itself for app-scoped
    grants — this only handles the global case."""
    raw = response.body if isinstance(response.body, bytes) else response.body.encode("utf-8")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return response

    required_grant = body.get("required_grant") if isinstance(body, dict) else None
    if not isinstance(required_grant, dict):
        return response
    if required_grant.get("scope", "global") != "global":
        return response
    grant = required_grant.get("grant")
    if not isinstance(grant, (str, dict)):
        return response

    required_grant["grant_url"] = _approve_grant_url(config, consumer_app_id, service_url, grant)
    return ASGIResponse(
        body=json.dumps(body).encode(),
        status_code=403,
        headers=list(_carry_response_headers(response.headers)),
        media_type=MediaType.JSON,
    )


def _carry_response_headers(headers: Any) -> Iterable[tuple[str, str]]:
    """Forward provider headers except framing ones that ASGIResponse owns itself."""
    for k, v in headers.items():
        if k.lower() in ("content-length", "content-type"):
            continue
        yield k, v


def _approve_grant_url(config: Config, consumer_app_id: str, service_url: str, grant: Any) -> str:
    approve_path = (
        f"/approve-permissions-v2?app={consumer_app_id}"
        f"&service={service_url}&grant={json.dumps(grant, sort_keys=True)}"
    )
    return f"https://{config.zone_domain}{approve_path}" if config.zone_domain else approve_path


# ─── Service call (HTTP) ────────────────────────────────────────────────────


@route(_CALL_PATH, http_method=_HTTP_METHODS, guards=[require_app_auth])
async def service_call(
    shortname: str,
    rest: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> ASGIResponse:
    consumer_app_id = _consumer_app_id_or_raise(request)
    """Proxy a request to the provider declared under <shortname> in the
    consumer's manifest.

    The response is streamed end-to-end except for 403s, which ``proxy_request``
    buffers so we can inject ``grant_url``.
    """
    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        return _asgi_json_error("shortname_not_declared", e.message, 404)

    if service_url == INSTALLER_SERVICE_URL:
        installer_response = await _handle_installer_request(consumer_app_id, version_spec, rest, request, db, config)
        return _response_to_asgi(installer_response, request)

    try:
        provider_app_id, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        return _asgi_json_error("service_not_available", e.message, 503)

    # `rest` is captured as "/sub/path" (leading slash); fold into the
    # provider's endpoint.
    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")

    proxied = await proxy_request(
        request,
        target_port=provider_port,
        override_path=target_path,
        extra_headers=[
            ("X-OpenHost-Permissions", _build_permissions_header(consumer_app_id, service_url, provider_app_id)),
            *_consumer_identity_headers(consumer_app_id, db).items(),
        ],
    )

    if proxied.status_code == 403:
        proxied = _inject_grant_url_if_global(proxied, service_url, consumer_app_id, config)

    return _apply_cors(proxied, request)


def _response_to_asgi(response: Response[Any], request: Request[Any, Any, Any]) -> ASGIResponse:
    """Bridge a high-level Litestar Response into an ASGIResponse, for handlers
    that return the low-level type (so 200/error paths can share a return shape)."""
    return response.to_asgi_response(app=request.app, request=request)


def _apply_cors(response: ASGIResponse, request: Request[Any, Any, Any]) -> ASGIResponse:
    origin = _cors_origin(request)
    if origin is None:
        return response
    # ASGIResponse stores headers on a MutableScopeHeaders; append the CORS set.
    for k, v in _cors_headers(origin).items():
        response.headers.add(k, v)
    return response


# ─── CORS preflight ─────────────────────────────────────────────────────────


@route(_CALL_PATH, http_method=[HttpMethod.OPTIONS], status_code=204)
async def service_call_cors(request: Request[Any, Any, Any], shortname: str, rest: str) -> Response[str]:
    del shortname, rest  # path-only routing
    origin = _cors_origin(request)
    if origin is None:
        return Response(content="Forbidden", status_code=403, media_type=MediaType.TEXT)
    return Response(content="", status_code=204, headers=_cors_headers(origin))


# ─── Service call (WebSocket) ───────────────────────────────────────────────


@websocket(_CALL_PATH)
async def service_call_ws(socket: WebSocket[Any, Any, Any], shortname: str, rest: str) -> None:
    """WebSocket variant of service_call.  Same auth + resolution; lifts the
    request/response proxy to ws_proxy."""
    consumer_app_id = resolve_caller_app_id(socket)
    if not consumer_app_id:
        await socket.accept()
        await socket.close(code=4401, reason="Missing or invalid authorization")
        return

    from compute_space.db import get_db  # noqa: PLC0415

    db = get_db()
    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except ShortnameNotDeclared as e:
        await socket.accept()
        await socket.close(code=4404, reason=e.message)
        return

    try:
        provider_app_id, provider_port, _, provider_endpoint = resolve_provider(service_url, version_spec, db)
    except ServiceNotAvailable as e:
        await socket.accept()
        await socket.close(code=4503, reason=e.message)
        return

    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")
    await ws_proxy(
        socket,
        target_port=provider_port,
        extra_headers=[
            ("X-OpenHost-Permissions", _build_permissions_header(consumer_app_id, service_url, provider_app_id)),
            *_consumer_identity_headers(consumer_app_id, db).items(),
        ],
        override_path=target_path,
    )


# ─── OAuth callback fan-out ─────────────────────────────────────────────────


@get("/api/services/v2/oauth_callback")
async def oauth_callback_proxy_v2(request: Request[Any, Any, Any]) -> ASGIResponse:
    """Proxy OAuth provider callbacks to the correct oauth service app.

    OAuth providers (Google, GitHub, etc.) redirect to a fixed callback URL on
    MY_REDIRECT_DOMAIN after user authorization. This endpoint receives that
    redirect and forwards it to the oauth app that initiated the flow.

    The oauth app encodes its app name in the OAuth ``state`` parameter as
    JSON: ``{"app": "<app_name>", "nonce": "<random>"}``. This endpoint parses
    that to determine which app should receive the callback, then proxies the
    full request to that app's ``/callback`` handler.
    """
    state_raw = request.query_params.get("state", "")
    if not state_raw:
        return _asgi_json_error("bad_request", "Missing state parameter", 400)

    try:
        state = json.loads(state_raw)
    except json.JSONDecodeError:
        return _asgi_json_error("bad_request", "Invalid state parameter", 400)

    app_name = state.get("app")
    if not app_name or not isinstance(app_name, str):
        return _asgi_json_error("bad_request", "Missing app in state", 400)

    app_row = find_app_by_name(app_name)
    if not app_row:
        return _asgi_json_error("service_not_available", f"App '{app_name}' not found", 503)
    if app_row.status != "running":
        return _asgi_json_error("service_not_available", f"App '{app_name}' is not running", 503)

    return await proxy_request(request, target_port=app_row.local_port, override_path="/callback")


# ─── Installer (router-internal v2 service) ─────────────────────────────────
#
# The installer has no provider app — its handlers run in-process so they can
# share the router's DB and apps.* state.  Apps that consume it declare:
#
#     [[services.v2.consumes]]
#     service   = "github.com/imbue-openhost/openhost/services/installer"
#     shortname = "installer"
#     version   = ">=0.1.0"
#     grants    = [{capability = "install", repo_url_prefix = "https://..."}]
#
# and call /api/services/v2/call/installer/{install,status/<name>,logs/<name>}.


async def _handle_installer_request(
    consumer_app_id: str,
    version_spec: str,
    rest: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> Response[Any]:
    """Dispatch installer v2 service requests in-process.

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

    method = str(request.method)
    parts = rest.strip("/").split("/")

    if method == "POST" and parts == ["install"]:
        try:
            body = await request.json()
        except Exception:
            return _json_error("bad_request", "request body must be JSON object", 400)
        if not isinstance(body, dict):
            return _json_error("bad_request", "request body must be JSON object", 400)
        repo_url = (body.get("repo_url") or "").strip()
        if not repo_url:
            return _json_error("bad_request", "repo_url is required", 400)
        app_name = (body.get("app_name") or "").strip() or None

        grants = [g.grant for g in get_granted_permissions_v2(consumer_app_id, INSTALLER_SERVICE_URL)]
        if (reason := check_install_allowed(repo_url, grants)) is not None:
            return _installer_permission_denied(consumer_app_id, repo_url, reason, db, config)

        try:
            result = await install_from_repo_url(repo_url, config, db, app_name=app_name, installed_by=consumer_app_id)
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
        logs = get_docker_logs(app_name, config.temporary_data_dir, row["container_id"])
        return Response(content=logs, status_code=200, media_type="text/plain; charset=utf-8")

    return _json_error("bad_request", f"Unknown installer endpoint: {method} /{rest.lstrip('/')}", 404)


def _lookup_consumer_install(
    consumer_app_id: str, app_name: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response[dict[str, Any]] | None]:
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
    """Pick the install grant payload to offer the owner on a 403.

    Prefers a grant the consumer **already declared** in its
    ``[[services.v2.consumes]]`` block for the installer service whose
    ``repo_url_prefix`` matches ``repo_url`` — so a manifest-declared broad
    grant (e.g. ``"https://github.com/"``) gets approved once and covers every
    subsequent install instead of producing one approval prompt per repo.

    Falls back to a per-URL grant only if the consumer's manifest declares no
    installer grants at all, or none whose prefix covers the requested URL.
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
                return {GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: prefix}
    return fallback


def _installer_permission_denied(
    consumer_app_id: str, repo_url: str, reason: str, db: sqlite3.Connection, config: Config
) -> Response[dict[str, Any]]:
    grant = _proposed_install_grant_from_manifest(consumer_app_id, repo_url, db)
    body = {
        "error": "permission_required",
        "message": reason,
        "required_grant": {
            "grant": grant,
            "scope": "global",
            "grant_url": _approve_grant_url(config, consumer_app_id, INSTALLER_SERVICE_URL, grant),
        },
    }
    return Response(content=body, status_code=403, media_type=MediaType.JSON)


# ─── Router ─────────────────────────────────────────────────────────────────


services_v2_routes = Router(
    path="/",
    route_handlers=[service_call, service_call_cors, service_call_ws, oauth_callback_proxy_v2],
)
