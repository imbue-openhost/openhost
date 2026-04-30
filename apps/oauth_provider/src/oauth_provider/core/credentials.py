import logging

import httpx

from oauth_provider.core.config import APP_TOKEN
from oauth_provider.core.config import ROUTER_URL
from oauth_provider.core.providers import PROVIDERS

log = logging.getLogger(__name__)

DYNAMIC_CRED_PROVIDERS = {"google"}

SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"
SECRETS_SERVICE_VERSION = ">=0.1.0"


class CredentialsNotAvailable(Exception):
    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        self.message = message
        super().__init__(message)


def get_provider_cred_key_names(provider_name: str) -> tuple[str, str]:
    p = provider_name.upper()
    return f"{p}_OAUTH_CLIENT_ID", f"{p}_OAUTH_CLIENT_SECRET"


async def get_provider_creds(provider_name: str) -> tuple[str, str]:
    """Get the credentials to make an oauth request to the provider's API.

    These typically come from registering an "app" with the provider in some fashion.
    """
    if provider_name in DYNAMIC_CRED_PROVIDERS:
        id_key, secret_key = get_provider_cred_key_names(provider_name)
        secrets = await _fetch_secrets([id_key, secret_key])
        client_id, client_secret = secrets.get(id_key), secrets.get(secret_key)
        if not client_id or not client_secret:
            raise CredentialsNotAvailable(provider_name, f"{id_key} and {secret_key} must be set in the secrets app")
    else:
        p = PROVIDERS[provider_name]
        client_id, client_secret = p.get("client_id"), p.get("client_secret")
        if not client_id or not client_secret:
            raise CredentialsNotAvailable(provider_name, f"Missing client_id or client_secret for {provider_name}")
    return client_id, client_secret


async def _fetch_secrets(keys: list[str]) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ROUTER_URL}/_services_v2/service_request",
            json={"keys": keys},
            headers={
                "Authorization": f"Bearer {APP_TOKEN}",
                "X-OpenHost-Service-URL": SECRETS_SERVICE_URL,
                "X-OpenHost-Service-Version": SECRETS_SERVICE_VERSION,
                "X-OpenHost-Service-Endpoint": "get",
            },
        )
        resp.raise_for_status()
        data: dict[str, str] = resp.json().get("secrets", {})
        return data
