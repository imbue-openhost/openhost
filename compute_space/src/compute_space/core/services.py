"""Cross-app service operations: provider lookup, OAuth token fetching."""

import httpx

from compute_space.core.util import assert_str
from compute_space.db import get_db


class ServiceNotAvailable(Exception):
    def __init__(self, message: str):
        self.message = message


def get_service_provider(service_name: str) -> tuple[str, int]:
    """Look up the provider app for a service, checking it's installed and running.

    Returns (app_id, local_port).

    Raises:
        ServiceNotAvailable: Service is not installed or not running.
    """
    db = get_db()
    row = db.execute(
        """SELECT sp.app_id, a.local_port, a.status FROM service_providers sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_name = ?""",
        (service_name,),
    ).fetchone()
    if not row:
        raise ServiceNotAvailable(f"No provider for service '{service_name}'. Is the '{service_name}' app installed?")
    if row["status"] != "running":
        raise ServiceNotAvailable(f"The '{service_name}' app is not running. Please start it from the dashboard.")
    app_id = row["app_id"]
    assert isinstance(app_id, str)
    app_port = row["local_port"]
    assert isinstance(app_port, int)
    return app_id, app_port


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
    _, provider_port = get_service_provider("secrets")
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
