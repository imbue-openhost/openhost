"""Server-side OAuth demo — redirect-based auth flow."""

import httpx
from oauth_demo.oauth import APP_NAME
from oauth_demo.oauth import GITHUB_SCOPE
from oauth_demo.oauth import GMAIL_SCOPE
from oauth_demo.oauth import MOCK_SCOPE
from oauth_demo.oauth import PROVIDERS
from oauth_demo.oauth import SCOPE_MAP
from oauth_demo.oauth import ZONE_DOMAIN
from oauth_demo.oauth import AuthRedirectRequired
from oauth_demo.oauth import OAuthError
from oauth_demo.oauth import OAuthServiceUnavailable
from oauth_demo.oauth import get_accounts
from oauth_demo.oauth import get_mock_provider_api_url
from oauth_demo.oauth import get_oauth_token
from quart import Blueprint
from quart import Response
from quart import redirect
from quart import render_template
from quart import request

server_bp = Blueprint("server", __name__, url_prefix="/server")


async def get_token_or_redirect(provider: str, scopes: list[str], account: str = "default") -> str | Response:
    """Get an OAuth token, or return a redirect/error Response if auth is needed."""
    return_to = f"//{APP_NAME}.{ZONE_DOMAIN}{request.full_path}"
    try:
        return await get_oauth_token(provider, scopes, account, return_to=return_to)
    except AuthRedirectRequired as e:
        return redirect(e.url)
    except (OAuthServiceUnavailable, OAuthError) as e:
        return await render_template("error.html", error=str(e))


@server_bp.route("/")
async def index():
    error = None
    providers = []
    for p in PROVIDERS:
        entry = {**p, "accounts": []}
        try:
            entry["accounts"] = await get_accounts(p["name"])
        except OAuthServiceUnavailable as e:
            return await render_template("secrets_required.html", error=str(e))
        except OAuthError as e:
            error = str(e)
        providers.append(entry)
    return await render_template("server/index.html", providers=providers, error=error)


@server_bp.route("/connect")
async def connect():
    """Redirect through OAuth to connect a new account."""
    provider = request.args.get("provider")
    account = request.args.get("account", "NEW")

    scopes = SCOPE_MAP.get(provider)
    if not scopes:
        return "Unknown provider", 400

    # Use /server/ as return_to so we don't loop back into connect after OAuth
    index_url = f"//{APP_NAME}.{ZONE_DOMAIN}/server/"
    try:
        await get_oauth_token(provider, scopes, account, return_to=index_url)
    except AuthRedirectRequired as e:
        return redirect(e.url)
    except (OAuthServiceUnavailable, OAuthError) as e:
        return await render_template("error.html", error=str(e))

    return redirect("/server/")


@server_bp.route("/unread")
async def unread():
    account = request.args.get("account", "default")

    result = await get_token_or_redirect("google", [GMAIL_SCOPE], account)
    if isinstance(result, Response):
        return result

    try:
        subjects = await _fetch_unread(result)
    except Exception as e:
        return await render_template("server/unread.html", error=str(e), subjects=None, account=account)

    return await render_template("server/unread.html", error=None, subjects=subjects, account=account)


@server_bp.route("/repos")
async def repos():
    account = request.args.get("account", "default")

    result = await get_token_or_redirect("github", [GITHUB_SCOPE], account)
    if isinstance(result, Response):
        return result

    try:
        repo_list = await _fetch_repos(result)
    except Exception as e:
        return await render_template("server/repos.html", error=str(e), repos=None)

    return await render_template("server/repos.html", error=None, repos=repo_list)


@server_bp.route("/emails")
async def emails():
    account = request.args.get("account", "default")

    result = await get_token_or_redirect("mock", [MOCK_SCOPE], account)
    if isinstance(result, Response):
        return result

    try:
        email_list = await _fetch_mock_emails(result)
    except Exception as e:
        return await render_template("server/emails.html", error=str(e), emails=None, account=account)

    return await render_template("server/emails.html", error=None, emails=email_list, account=account)


async def _fetch_mock_emails(access_token: str) -> list[dict]:
    api_url = get_mock_provider_api_url()
    if not api_url:
        raise RuntimeError("Mock provider API URL not configured")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/api/emails",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Mock API error: {resp.text}")
    return resp.json().get("emails", [])


async def _fetch_unread(access_token: str) -> list[str]:
    async with httpx.AsyncClient() as client:
        list_resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params={"q": "is:unread", "maxResults": "20"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if list_resp.status_code != 200:
        raise RuntimeError(f"Gmail API error: {list_resp.text}")

    messages = list_resp.json().get("messages", [])
    subjects = []
    async with httpx.AsyncClient() as client:
        for msg in messages:
            detail = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                params={"format": "metadata", "metadataHeaders": "Subject"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if detail.status_code == 200:
                hdrs = detail.json().get("payload", {}).get("headers", [])
                subject = next(
                    (h["value"] for h in hdrs if h["name"] == "Subject"),
                    "(no subject)",
                )
                subjects.append(subject)
    return subjects


async def _fetch_repos(access_token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user/repos",
            params={"sort": "updated", "per_page": "30"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub API error: {resp.text}")
    return resp.json()
