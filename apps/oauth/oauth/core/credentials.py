import logging

import httpx

from oauth.core.config import APP_TOKEN
from oauth.core.config import ROUTER_URL
from oauth.core.providers import PROVIDERS

log = logging.getLogger(__name__)

DYNAMIC_CRED_PROVIDERS = {"google"}


def provider_cred_keys(provider_name: str) -> tuple[str, str]:
    p = provider_name.upper()
    return f"{p}_OAUTH_CLIENT_ID", f"{p}_OAUTH_CLIENT_SECRET"


async def get_provider_creds(provider_name: str) -> tuple[str | None, str | None]:
    if provider_name in DYNAMIC_CRED_PROVIDERS:
        id_key, secret_key = provider_cred_keys(provider_name)
        secrets = await _fetch_secrets([id_key, secret_key])
        return secrets.get(id_key), secrets.get(secret_key)
    p = PROVIDERS[provider_name]
    return p.get("client_id"), p.get("client_secret")


async def _fetch_secrets(keys: list[str]) -> dict[str, str]:
    if not ROUTER_URL:
        raise RuntimeError("OPENHOST_ROUTER_URL not set — cannot fetch provider credentials")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ROUTER_URL}/secrets/_service/get",
            json={"keys": keys},
            headers={"Authorization": f"Bearer {APP_TOKEN}"},
        )
        resp.raise_for_status()
        data: dict[str, str] = resp.json().get("secrets", {})
        return data
