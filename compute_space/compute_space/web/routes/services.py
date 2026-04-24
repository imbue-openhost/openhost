import json
from urllib.parse import urlparse

from quart import Blueprint
from quart import Response
from quart import request
from quart import url_for

from compute_space.config import get_config
from compute_space.core.permissions import get_granted_permissions
from compute_space.core.service_access_rules import ServiceAccessDenied
from compute_space.core.service_access_rules import check_service_access_rules
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_service_provider
from compute_space.web.middleware import app_auth_required
from compute_space.web.proxy import proxy_request

services_bp = Blueprint("services", __name__)


# ─── Cross-App Services Proxy ───


def _app_subdomain_from_origin() -> tuple[str | None, str | None]:
    """Extract app name from Origin/Referer if it's a valid app subdomain."""
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        if not referer:
            return None, None
        origin = referer

    parsed = urlparse(origin)
    hostname = parsed.hostname or ""
    raw_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else None

    config = get_config()
    if not config.zone_domain or not hostname.endswith("." + config.zone_domain):
        return None, raw_origin

    app_name = hostname[: -(len(config.zone_domain) + 1)]
    if "." in app_name:
        return None, raw_origin

    return app_name, raw_origin


def _cors_origin() -> str | None:
    """Return the Origin if it's a valid app subdomain, for CORS headers."""
    app_name, raw_origin = _app_subdomain_from_origin()
    return raw_origin if app_name else None


def _add_cors_headers(response: Response, origin: str) -> Response:
    """Add CORS headers for cross-origin requests from app subdomains."""
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, "
        "X-OpenHost-Service-URL, X-OpenHost-Service-Version, "
        "X-OpenHost-Service-Endpoint, X-OpenHost-Provider-App"
    )
    return response


@services_bp.after_request
async def add_cors_to_services(response: Response) -> Response:
    """Add CORS headers to /_services/ responses for browser requests."""
    if request.path.startswith("/_services/"):
        origin = _cors_origin()
        if origin:
            _add_cors_headers(response, origin)
    return response


@services_bp.route(
    "/_services/<service_name>/<path:service_endpoint>",
    methods=["OPTIONS"],
)
async def service_proxy_cors(service_name: str, service_endpoint: str) -> Response:
    """Handle CORS preflight for browser requests to /_services/."""
    origin = _cors_origin()
    if not origin:
        return Response("Forbidden", status=403)
    return _add_cors_headers(Response("", status=204), origin)


@services_bp.route(
    "/_services/<service_name>/<path:service_endpoint>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
@app_auth_required
async def service_proxy(service_name: str, service_endpoint: str, app_name: str) -> Response:
    """Proxy a cross-app service request from a consumer app to the provider app.

    The request goes to the provider app at `/_service/<service_endpoint>`
    """
    consumer_app = app_name

    try:
        provider_app, provider_port = get_service_provider(service_name)
    except ServiceNotAvailable as e:
        return _json_error("service_not_available", e.message, 503)

    try:
        required_permissions = await check_service_access_rules(service_name, service_endpoint, request)
    except ServiceAccessDenied as e:
        return _json_error("forbidden", e.message, 403)

    if required_permissions:
        permissions_granted = get_granted_permissions(consumer_app)
        permissions_needed = [k for k in required_permissions if k not in permissions_granted]
        if permissions_needed:
            # Read return_to from request body if present, for redirect after approval
            try:
                body = await request.get_json()
                return_to = body.get("return_to", "") if body else ""
            except Exception:
                return_to = ""
            config = get_config()
            approve_path = url_for(
                "pages_permissions.approve_permissions",
                app=consumer_app,
                permissions=",".join(permissions_needed),
                return_to=return_to,
            )
            grant_url = f"https://{config.zone_domain}{approve_path}"
            return Response(
                json.dumps(
                    {
                        "error": "permission_denied",
                        "denied_keys": permissions_needed,
                        "grant_url": grant_url,
                    }
                ),
                status=403,
                content_type="application/json",
            )

    return await proxy_request(
        request,
        provider_port,
        "",
        override_path=f"/_service/{service_endpoint}",
        extra_headers={"Authorization": None},
    )


def _json_error(error: str, message: str, status: int) -> Response:
    return Response(
        json.dumps({"error": error, "message": message}),
        status=status,
        content_type="application/json",
    )


# ─── OAuth Callback Proxy ───


@services_bp.route("/secrets/oauth/callback")
async def oauth_callback_proxy() -> Response:
    try:
        provider_app, provider_port = get_service_provider("secrets")
    except ServiceNotAvailable as e:
        return _json_error("service_not_available", e.message, 503)
    return await proxy_request(request, provider_port, "", override_path="/oauth/callback")
