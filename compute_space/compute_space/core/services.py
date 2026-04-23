"""Cross-app service operations: provider lookup, OAuth token fetching."""

import httpx
from sqlalchemy import select

from compute_space.core.util import assert_str
from compute_space.db import get_session
from compute_space.db.models import App
from compute_space.db.models import ServiceProvider


class ServiceNotAvailable(Exception):
    def __init__(self, message: str):
        self.message = message


async def get_service_provider(service_name: str) -> tuple[str, int]:
    """Look up the provider app for a service, checking it's installed and running.

    Returns (app_name, local_port).

    Raises:
        ServiceNotAvailable: Service is not installed or not running.
    """
    session = get_session()
    stmt = (
        select(ServiceProvider.app_name, App.local_port, App.status)
        .join(App, App.name == ServiceProvider.app_name)
        .where(ServiceProvider.service_name == service_name)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise ServiceNotAvailable(f"No provider for service '{service_name}'. Is the '{service_name}' app installed?")
    app_name, local_port, status = row
    if status != "running":
        raise ServiceNotAvailable(f"The '{service_name}' app is not running. Please start it from the dashboard.")
    assert isinstance(app_name, str)
    assert isinstance(local_port, int)
    return app_name, local_port


class OAuthAuthorizationRequired(Exception):
    def __init__(self, authorize_url: str):
        self.authorize_url = authorize_url


async def get_oauth_token(
    provider: str,
    scopes: list[str],
    return_to: str,
) -> str:
    """Get an OAuth token from the secrets service for the given provider and scopes.

    Args:
        provider: OAuth provider name (e.g. "github", "google").
        scopes: OAuth scopes to request (e.g. ["repo"]).
        return_to: URL to redirect to after the user completes authorization.
            Included in the OAuthAuthorizationRequired flow URL.

    Returns the access token string.

    Raises:
        ServiceNotAvailable: The secrets service is not installed, not running, or not responding.
        OAuthAuthorizationRequired: User authorization is needed (has authorize_url).
    """
    _, provider_port = await get_service_provider("secrets")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"http://127.0.0.1:{provider_port}/_service/oauth/token",
                json={
                    "provider": provider,
                    "scopes": scopes,
                    "return_to": return_to,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as e:
        raise ServiceNotAvailable(f"Secrets service is not responding: {e}") from e
    data = resp.json()
    if resp.status_code == 200:
        return assert_str(data["access_token"])
    if resp.status_code == 401 and data.get("status") == "authorization_required":
        raise OAuthAuthorizationRequired(data["authorize_url"])
    raise ServiceNotAvailable(f"Secrets service returned unexpected status {resp.status_code}: {resp.text}")
