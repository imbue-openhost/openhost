import os

import httpx

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN")
APP_NAME = os.environ.get("OPENHOST_APP_NAME")
ZONE_DOMAIN = os.environ.get("OPENHOST_ZONE_DOMAIN")

OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth"
SERVICE_REQUEST_URL = f"{ROUTER_URL}/_services_v2/service_request"

_mock_base_url: str | None = None


def set_mock_oauth_url(url: str | None) -> None:
    global _mock_base_url
    _mock_base_url = url.rstrip("/") if url else None


def _request_url() -> str:
    return _mock_base_url or SERVICE_REQUEST_URL


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GITHUB_SCOPE = "repo"

AUTH_HEADERS = {"Authorization": f"Bearer {APP_TOKEN}"}

SCOPE_MAP = {
    "google": [GMAIL_SCOPE],
    "github": [GITHUB_SCOPE],
}

PROVIDERS = [
    {
        "name": "google",
        "label": "Google",
        "action_path": "unread",
        "action_label": "Emails",
    },
    {
        "name": "github",
        "label": "GitHub",
        "action_path": "repos",
        "action_label": "Repos",
    },
]


class AuthRedirectRequired(Exception):
    """User needs to visit a URL to grant permissions or complete OAuth consent."""

    def __init__(self, url: str):
        self.url = url


class OAuthError(Exception):
    """OAuth token request failed with a displayable error."""


class OAuthServiceUnavailable(Exception):
    """The OAuth service is not installed or not running."""


class _PermissionDenied(Exception):
    def __init__(self, grant_url: str):
        self.grant_url = grant_url


class _OAuthConsentRequired(Exception):
    def __init__(self, authorize_url: str):
        self.authorize_url = authorize_url


def _check_error_response(resp: httpx.Response) -> None:
    """Raise a typed exception for any non-success response."""
    if resp.is_success:
        return
    try:
        data = resp.json()
    except Exception as e:
        raise OAuthError(f"Request failed: {resp.status_code} {resp.text}") from e
    error = data.get("error")
    if error in ("service_not_found", "service_not_running", "service_not_available"):
        raise OAuthServiceUnavailable(data.get("message", "OAuth service unavailable"))
    # V2 permission_required (from provider via router)
    if error == "permission_required":
        grant = data.get("required_grant", {})
        raise _PermissionDenied(grant_url=grant.get("grant_url", ""))
    # V1 permission_denied
    if error == "permission_denied":
        raise _PermissionDenied(grant_url=data.get("grant_url", ""))
    if resp.status_code == 401 and data.get("authorize_url"):
        raise _OAuthConsentRequired(data["authorize_url"])
    raise OAuthError(data.get("message", f"Request failed: {resp.status_code}"))


async def get_accounts(provider: str) -> list[str]:
    """Get list of connected account labels for a provider.

    Returns empty list if permissions haven't been granted yet.
    """
    scopes = SCOPE_MAP.get(provider)
    if not scopes:
        raise ValueError(f"Unknown provider: {provider}")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _request_url(),
            json={"provider": provider, "scopes": scopes},
            headers={
                **AUTH_HEADERS,
                "X-OpenHost-Service-URL": OAUTH_SERVICE_URL,
                "X-OpenHost-Service-Version": ">=0.1.0",
                "X-OpenHost-Service-Endpoint": "accounts",
            },
        )
        try:
            _check_error_response(resp)
        except _PermissionDenied:
            return []
        return resp.json().get("accounts", [])


async def get_oauth_token(provider: str, scopes: list[str], account: str = "default", return_to: str = "") -> str:
    """Get an OAuth access token from the secrets service.

    Args:
        return_to: URL to redirect back to after auth completes.
                   Defaults to //{APP_NAME}.{ZONE_DOMAIN}/client/oauth-complete.

    Returns the access token string.

    Raises:
        AuthRedirectRequired: User needs to visit a URL (permissions or OAuth consent).
        OAuthServiceUnavailable: The OAuth service is not installed or not running.
        OAuthError: Other failures.
    """
    if not return_to:
        return_to = f"//{APP_NAME}.{ZONE_DOMAIN}/client/oauth-complete"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _request_url(),
                json={
                    "provider": provider,
                    "scopes": scopes,
                    "return_to": return_to,
                    "account": account,
                },
                headers={
                    **AUTH_HEADERS,
                    "X-OpenHost-Service-URL": OAUTH_SERVICE_URL,
                    "X-OpenHost-Service-Version": ">=0.1.0",
                    "X-OpenHost-Service-Endpoint": "token",
                },
            )
    except httpx.HTTPError as e:
        raise OAuthError(f"Token request failed: {e}") from e

    try:
        _check_error_response(resp)
    except _PermissionDenied as e:
        raise AuthRedirectRequired(e.grant_url) from e
    except _OAuthConsentRequired as e:
        raise AuthRedirectRequired(e.authorize_url) from e

    return resp.json()["access_token"]
