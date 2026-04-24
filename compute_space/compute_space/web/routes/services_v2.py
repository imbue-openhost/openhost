"""V2 cross-app service proxy: git-URL-based service identity, versioned routing,
provider-side permission validation."""

import json
from urllib.parse import unquote

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
from compute_space.web.proxy import proxy_request
from compute_space.web.routes.services import _add_cors_headers
from compute_space.web.routes.services import _authenticate_and_resolve_consumer_app
from compute_space.web.routes.services import _cors_origin

services_v2_bp = Blueprint("services_v2", __name__)

PREFIX = b"/_services_v2/"


def _parse_service_url_and_endpoint(raw_path: bytes) -> tuple[str, str]:
    """Split raw ASGI path into (service_url, endpoint).

    The service URL is URL-encoded (internal slashes as %2F), so the first
    literal '/' after the prefix separates service URL from endpoint.
    """
    after_prefix = raw_path[len(PREFIX) :]
    # Strip query string
    if b"?" in after_prefix:
        after_prefix = after_prefix[: after_prefix.index(b"?")]
    slash_idx = after_prefix.find(b"/")
    if slash_idx == -1:
        return unquote(after_prefix.decode("ascii")), ""
    service_url_encoded = after_prefix[:slash_idx]
    endpoint = after_prefix[slash_idx + 1 :]
    return unquote(service_url_encoded.decode("ascii")), endpoint.decode("ascii")


# ─── CORS ───


@services_v2_bp.after_request
async def add_cors_to_services_v2(response: Response) -> Response:
    if request.path.startswith("/_services_v2/"):
        origin = _cors_origin()
        if origin:
            _add_cors_headers(response, origin)
    return response


@services_v2_bp.route("/_services_v2/<path:rest>", methods=["OPTIONS"])
async def service_v2_cors(rest: str) -> Response:
    origin = _cors_origin()
    if not origin:
        return Response("Forbidden", status=403)
    return _add_cors_headers(Response("", status=204), origin)


# ─── Proxy ───


@services_v2_bp.route(
    "/_services_v2/<path:rest>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def service_v2_proxy(rest: str) -> Response:
    """Proxy a V2 service request to the resolved provider."""
    consumer_app = _authenticate_and_resolve_consumer_app()
    if not consumer_app:
        return Response("Missing or invalid authorization", status=401)

    raw_path = request.scope.get("raw_path", b"")
    service_url, endpoint = _parse_service_url_and_endpoint(raw_path)
    if not service_url:
        return _json_error("bad_request", "Missing service URL in path", 400)

    version_spec = request.args.get("version")
    if not version_spec:
        return _json_error("bad_request", "Missing required 'version' query parameter", 400)

    provider_app = request.args.get("provider_app")

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

    target_path = provider_endpoint.rstrip("/") + "/" + endpoint if endpoint else provider_endpoint

    response = await proxy_request(
        request,
        provider_port,
        "",
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
