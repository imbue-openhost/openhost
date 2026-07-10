from litestar import Response
from litestar import Router
from litestar import get

from compute_space.core.apps import find_app_by_alt_domain


@get("/api/tls/on_demand_check")
async def on_demand_check(domain: str) -> Response[str]:
    """Caddy on-demand TLS "ask" hook: 2xx allows cert issuance for ``domain``, anything else refuses.

    Unauthenticated by design — Caddy calls it over loopback before any TLS handshake completes, and it
    reveals only whether a hostname is a registered alternate domain.
    """
    host = domain.strip().lower().rstrip(".")
    if host and find_app_by_alt_domain(host) is not None:
        return Response(content="", status_code=200)
    return Response(content="", status_code=404)


api_tls_routes = Router(
    path="/",
    route_handlers=[on_demand_check],
)
