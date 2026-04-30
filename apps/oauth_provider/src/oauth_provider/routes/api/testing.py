from litestar import Router
from litestar import post

import oauth_provider.core.config as config
import oauth_provider.core.providers as providers_mod
from oauth_provider.core.models import MockProviderUrlData
from oauth_provider.core.models import OkResponse


@post("/test/set-mock-provider-url")
async def set_mock_provider_url(data: MockProviderUrlData) -> OkResponse:
    """When using this app as part of a test suite, this route can be used to set the URLs for the mock provider."""
    assert data.provider in ("mock", "mock_device")

    key_map = {
        "authorize_url": "auth_url",
        "device_url": "device_code_url",
        "token_url": "token_url",
        "revoke_url": "revoke_url",
    }
    for attr_name, provider_key in key_map.items():
        value = getattr(data, attr_name)
        if value:
            providers_mod.PROVIDERS[data.provider][provider_key] = value

    if data.userinfo_url:
        providers_mod.USERINFO_URLS[data.provider] = (
            data.userinfo_url,
            data.userinfo_field,
        )

    config.OAUTH_REDIRECT_URI = data.redirect_uri

    return OkResponse()


router = Router(path="", route_handlers=[set_mock_provider_url])
