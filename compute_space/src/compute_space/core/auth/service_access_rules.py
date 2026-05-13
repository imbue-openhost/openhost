"""Service-specific access rules for the cross-app service proxy.

Each service can define a handler that inspects the endpoint and request,
returning the list of permission keys required (empty = no check needed)
or raising ServiceAccessDenied to deny the request.
"""

from typing import Any
from typing import Protocol


class ServiceRequest(Protocol):
    """Framework-neutral view of an HTTP request used by service access rules."""

    method: str

    async def get_json(self) -> Any: ...


class ServiceAccessDenied(Exception):
    def __init__(self, message: str):
        self.message = message


async def check_service_access_rules(service_name: str, service_endpoint: str, req: ServiceRequest) -> list[str]:
    """Determine what permissions are required for a service proxy request.

    Returns:
        list[str]: Required permission keys (empty list = allowed, no permissions needed). Key should generally have the service name prefixed (eg "secrets/")

    Raises:
        ServiceAccessDenied: Request is not allowed.
    """
    handler = _SERVICE_ACCESS_RULES.get(service_name)
    if handler:
        return await handler(service_endpoint, req)
    raise ServiceAccessDenied(f"No access rules defined for service '{service_name}'")


async def _get_json_body(req: ServiceRequest) -> dict[Any, Any]:
    """Parse JSON body or raise ServiceAccessDenied on failure."""
    try:
        data = await req.get_json()
    except Exception as e:
        raise ServiceAccessDenied("Invalid or missing JSON body") from e
    if not isinstance(data, dict):
        raise ServiceAccessDenied("Invalid or missing JSON body")
    return data


async def _secrets_access_rules(service_endpoint: str, req: ServiceRequest) -> list[str]:
    """Access rules for the secrets service. Whitelist of allowed endpoints.

    Returns a list of required permissions.
    """
    if service_endpoint == "list" and req.method == "GET":
        return []

    if service_endpoint == "get" and req.method == "POST":
        data = await _get_json_body(req)
        keys = data.get("keys", [])
        if not keys:
            raise ServiceAccessDenied("Missing 'keys' in request body")
        return [f"secrets/key:{key}" for key in keys]

    if service_endpoint in ("oauth/token", "oauth/accounts") and req.method == "POST":
        data = await _get_json_body(req)
        provider = data.get("provider", "")
        scopes = data.get("scopes", [])
        if not provider or not scopes:
            raise ServiceAccessDenied("Missing 'provider' or 'scopes' in request body")
        return [f"secrets/oauth:{provider}:{s}" for s in scopes]

    raise ServiceAccessDenied(f"Endpoint '{service_endpoint}' is not available on the 'secrets' service")


_SERVICE_ACCESS_RULES = {
    "secrets": _secrets_access_rules,
}
