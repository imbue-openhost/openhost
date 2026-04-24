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
from compute_space.web.routes.services import _add_cors_headers
from compute_space.web.routes.services import _cors_origin

services_v2_bp = Blueprint("services_v2", __name__)


# ─── CORS ───


@services_v2_bp.after_request
async def add_cors_to_services_v2(response: Response) -> Response:
    if request.path == "/_services_v2/service_request":
        origin = _cors_origin()
        if origin:
            _add_cors_headers(response, origin)
    return response


@services_v2_bp.route("/_services_v2/service_request", methods=["OPTIONS"])
async def service_v2_cors() -> Response:
    origin = _cors_origin()
    if not origin:
        return Response("Forbidden", status=403)
    return _add_cors_headers(Response("", status=204), origin)


# ─── Proxy ───


@services_v2_bp.route(
    "/_services_v2/service_request",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
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

    Returns 403 with grant/approve URLs if the provider rejects the request
    for missing permissions.
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
            "X-OpenHost-Permissions": grants_json,
            "X-OpenHost-Consumer": consumer_app,
        },
    )

    if response.status_code == 403:
        return await _maybe_reform_403(response, service_url, consumer_app)

    return response


async def _maybe_reform_403(
    response: Response,
    service_url: str,
    consumer_app: str,
) -> Response:
    """If the provider returned a 403 with required_grants, reform the response
    with approve/grant URLs. Otherwise pass through as-is."""
    try:
        body = json.loads(await response.get_data())
    except (json.JSONDecodeError, Exception):
        return response

    required_grants = body.get("required_grants")
    if not isinstance(required_grants, list):
        return response

    config = get_config()
    grants_needed = []
    for grant in required_grants:
        scope = grant.get("scope", "global")
        entry: dict[str, str] = {"key": grant.get("key", ""), "scope": scope}
        if scope == "app" and "grant_url" in grant:
            entry["grant_url"] = grant["grant_url"]
        else:
            approve_path = url_for(
                "pages_permissions_v2.approve_permissions_v2",
                app=consumer_app,
                service=service_url,
                grant=json.dumps(grant, sort_keys=True),
            )
            entry["approve_url"] = f"https://{config.zone_domain}{approve_path}"
        grants_needed.append(entry)

    return Response(
        json.dumps(
            {
                "error": "permission_required",
                "grants_needed": grants_needed,
                "service": service_url,
            }
        ),
        status=403,
        content_type="application/json",
    )


def _json_error(error: str, message: str, status: int) -> Response:
    return Response(
        json.dumps({"error": error, "message": message}),
        status=status,
        content_type="application/json",
    )
