"""V2 cross-app service proxy: git-URL-based service identity, versioned routing,
provider-side permission validation."""

import json

import attr
from quart import Blueprint
from quart import Response
from quart import request
from quart import url_for

from compute_space.config import get_config
from compute_space.core.permissions_v2 import get_granted_permissions_v2
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services_v2 import resolve_provider
from compute_space.db import get_db
from compute_space.web.middleware import app_auth_required
from compute_space.web.proxy import proxy_request
from compute_space.web.routes.proxy import _find_app_by_name
from compute_space.web.routes.services import _add_cors_headers
from compute_space.web.routes.services import _cors_origin

services_v2_bp = Blueprint("services_v2", __name__)


# ─── CORS ───


@services_v2_bp.after_request
async def add_cors_to_services_v2(response: Response) -> Response:
    if request.path == "/api/services/v2/service_request":
        origin = _cors_origin()
        if origin:
            _add_cors_headers(response, origin)
    return response


@services_v2_bp.route("/api/services/v2/service_request", methods=["OPTIONS"])
async def service_v2_cors() -> Response:
    origin = _cors_origin()
    if not origin:
        return Response("Forbidden", status=403)
    return _add_cors_headers(Response("", status=204), origin)


# ─── Proxy ───


@services_v2_bp.route(
    "/api/services/v2/service_request",
    methods=["GET", "POST"],
)
@app_auth_required
async def service_v2_proxy(app_name: str) -> Response:
    """Proxy a V2 service request to the provider that implements it.

    Resolves a service by service URL and version, looks up the consumer's granted
    permissions, and forwards the request to the provider's local port.
    The provider receives the caller's permissions in X-OpenHost-Permissions
    so it can enforce access control itself.

    Required headers:
        X-OpenHost-Service-URL:      Service identifier (e.g. github.com/org/repo/services/secrets).
        X-OpenHost-Service-Version:  SemVer specifier (e.g. >=0.1.0, ==1.0.0).
        X-OpenHost-Service-Endpoint: Path (and optional query string) to forward to the provider.

    Optional headers:
        X-OpenHost-Provider-App:     Pin to a specific provider app (default: use service_defaults).

    If a permissions is needed, provider apps should instead return a 403 with body
    Global:  {"error": "permission_required", "required_grant": { "grant_payload": ..., "scope": "global" }}
    App:     {"error": "permission_required", "required_grant": { "grant_payload": ...,
                 "scope": "app", "grant_url": "https://..." }}
    For app-scoped grants, grant_url must be provided
    and should be a link through the service proxy to an approval page for the required grant.
    We will add a grant_url for global grants that points to a compute space-provided approval page.
    """
    consumer_app = app_name

    if not (service_url := request.headers.get("X-OpenHost-Service-URL", "")):
        return _json_error("bad_request", "Missing X-OpenHost-Service-URL header", 400)

    if not (version_spec := request.headers.get("X-OpenHost-Service-Version", "")):
        return _json_error("bad_request", "Missing X-OpenHost-Service-Version header", 400)

    if not (endpoint := request.headers.get("X-OpenHost-Service-Endpoint", "")):
        return _json_error("bad_request", "Missing X-OpenHost-Service-Endpoint header", 400)

    provider_app = request.headers.get("X-OpenHost-Provider-App")

    db = get_db()
    try:
        app_name, provider_port, version, provider_endpoint = resolve_provider(
            service_url,
            version_spec,
            db,
            provider_app=provider_app,
        )
    except ServiceNotAvailable as e:
        return _json_error("service_not_available", e.message, 503)

    grants = get_granted_permissions_v2(consumer_app, service_url)
    grants_json = json.dumps([attr.asdict(g) for g in grants])

    target_path = provider_endpoint.rstrip("/") + "/" + endpoint.lstrip("/")

    response = await proxy_request(
        request,
        provider_port,
        base_path="",
        override_path=target_path,
        extra_headers={
            "Authorization": None,
            "Bearer": None,
            "X-OpenHost-Permissions": grants_json,
            "X-OpenHost-Consumer": consumer_app,
        },
    )

    if response.status_code == 403:
        return await _add_grant_url_if_global_grant_needed(response, service_url, consumer_app)

    return response


async def _add_grant_url_if_global_grant_needed(
    response: Response,
    service_url: str,
    consumer_app: str,
) -> Response:
    """Add grant_url to service provider 403 responses that indicate a missing globally-scoped permission grant.

    Providers return 403 when the consumer lacks a required permission. Expected format:
        Global:  {"error": "permission_required", "required_grant": { "grant_payload": ..., "scope": "global" }}
        App:     {"error": "permission_required", "required_grant": { "grant_payload": ...,
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

    grant_payload = required_grant.get("grant_payload")
    if not isinstance(grant_payload, dict):
        # we can't make a grant URL without a valid grant payload, so just return the original response even if it's malformed
        return response

    config = get_config()
    approve_path = url_for(
        "pages_permissions_v2.approve_permissions_v2",
        app=consumer_app,
        service=service_url,
        grant=json.dumps(grant_payload, sort_keys=True),
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

    app_row = _find_app_by_name(app_name)
    if not app_row:
        return _json_error("service_not_available", f"App '{app_name}' not found", 503)
    if app_row["status"] != "running":
        return _json_error("service_not_available", f"App '{app_name}' is not running", 503)

    return await proxy_request(request, app_row["local_port"], "", override_path="/callback")
