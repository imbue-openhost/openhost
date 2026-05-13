from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar import route
from litestar.handlers import HTTPRouteHandler

from compute_space.config import get_config
from compute_space.core.auth.permissions import get_granted_permissions
from compute_space.core.auth.service_access_rules import ServiceAccessDenied
from compute_space.core.auth.service_access_rules import check_service_access_rules
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_service_provider
from compute_space.web.proxy import proxy_request


def _app_subdomain_from_origin(request: Request[Any, Any, Any]) -> tuple[str | None, str | None]:
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        if not referer:
            return None, None
        origin = referer
    parsed = urlparse(origin)
    host = parsed.netloc or ""
    raw_origin = f"{parsed.scheme}://{host}" if parsed.scheme else None
    config = get_config()
    if not config.zone_domain or not host.endswith("." + config.zone_domain):
        return None, raw_origin
    app_name = host[: -(len(config.zone_domain) + 1)]
    if "." in app_name:
        return None, raw_origin
    return app_name, raw_origin


def _cors_origin(request: Request[Any, Any, Any]) -> str | None:
    app_name, raw_origin = _app_subdomain_from_origin(request)
    return raw_origin if app_name else None


def _add_cors_headers(response: Response[Any], origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"


@route("/_services/{service_name:str}/{service_endpoint:path}", http_method=["OPTIONS"])
async def service_proxy_cors(
    request: Request[Any, Any, Any], service_name: str, service_endpoint: str
) -> Response[bytes]:
    origin = _cors_origin(request)
    if not origin:
        return Response(content=b"Forbidden", status_code=403, media_type="text/plain")
    response: Response[bytes] = Response(content=b"", status_code=204)
    _add_cors_headers(response, origin)
    return response


def _add_cors_to_request(request: Request[Any, Any, Any], response: Response[Any]) -> Response[Any]:
    if request.url.path.startswith("/_services/"):
        origin = _cors_origin(request)
        if origin:
            _add_cors_headers(response, origin)
    return response


@route(
    "/_services/{service_name:str}/{service_endpoint:path}",
    http_method=["GET", "POST", "PUT", "DELETE", "PATCH"],
    status_code=200,
)
async def service_proxy(
    request: Request[Any, Any, Any],
    service_name: str,
    service_endpoint: str,
    caller_app_id: str,
) -> Response[Any]:
    """Proxy a cross-app service request from a consumer app to the provider app."""
    consumer_app_id = caller_app_id
    # service_endpoint comes from path:str; strip leading slash if present.
    service_endpoint = service_endpoint.lstrip("/")
    response: Response[Any]

    try:
        _provider_app_id, provider_port = get_service_provider(service_name)
    except ServiceNotAvailable as e:
        response = _json_error("service_not_available", e.message, 503)
        return _add_cors_to_request(request, response)

    try:
        required_permissions = await check_service_access_rules(
            service_name,
            service_endpoint,
            request,  # type: ignore[arg-type]
        )
    except ServiceAccessDenied as e:
        response = _json_error("forbidden", e.message, 403)
        return _add_cors_to_request(request, response)

    if required_permissions:
        permissions_granted = get_granted_permissions(consumer_app_id)
        permissions_needed = [k for k in required_permissions if k not in permissions_granted]
        if permissions_needed:
            try:
                body = await request.json()
                return_to = body.get("return_to", "") if body else ""
            except Exception:
                return_to = ""
            config = get_config()
            qs = urlencode(
                {
                    "app": consumer_app_id,
                    "permissions": ",".join(permissions_needed),
                    "return_to": return_to or "",
                }
            )
            grant_url = f"https://{config.zone_domain}/approve-permissions?{qs}"
            response = Response(
                content={
                    "error": "permission_denied",
                    "denied_keys": permissions_needed,
                    "grant_url": grant_url,
                },
                status_code=403,
            )
            return _add_cors_to_request(request, response)

    response = await proxy_request(
        request.scope,
        request.receive,
        provider_port,
        override_path=f"/_service/{service_endpoint}",
        extra_headers={"Authorization": None},
    )
    return _add_cors_to_request(request, response)


def _json_error(error: str, message: str, status: int) -> Response[dict[str, Any]]:
    return Response(content={"error": error, "message": message}, status_code=status)


@route("/secrets/oauth/callback", http_method=["GET", "POST"], status_code=200)
async def oauth_callback_proxy(request: Request[Any, Any, Any]) -> Response[Any]:
    try:
        _provider_app_id, provider_port = get_service_provider("secrets")
    except ServiceNotAvailable as e:
        return _json_error("service_not_available", e.message, 503)
    return await proxy_request(
        request.scope,
        request.receive,
        provider_port,
        override_path="/oauth/callback",
    )


services_routes: list[HTTPRouteHandler] = [service_proxy_cors, service_proxy, oauth_callback_proxy]
