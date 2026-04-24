"""OAuth flow e2e tests — multi-account support, authorization redirects, and
a full Playwright browser test through the mock OAuth provider.

Requires Docker (--run-docker) and Playwright (chromium browser).
"""

import asyncio
import os
import threading

import pytest
import requests
from playwright.sync_api import sync_playwright

from compute_space.testing import poll
from compute_space.testing import wait_app_running

from . import mock_oauth_server as mock_oauth_server_module
from .conftest import MOCK_OAUTH_PORT
from .conftest import ZONE_DOMAIN
from .conftest import _APPS_DIR
from .conftest import requires_docker

_OAUTH_DEMO_DIR = os.path.join(_APPS_DIR, "oauth_demo")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_oauth_server(secrets_app_deployed):
    """Configure the secrets app to use the mock OAuth server as the 'mock' provider.

    Overrides the mock provider's auth/token/userinfo/revoke URLs so the full
    OAuth redirect flow goes through the mock server instead of a real provider.
    """
    mock_oauth_server_module.reset()
    mock_oauth_server_module.authorize_base_url = f"http://host.docker.internal:{MOCK_OAUTH_PORT}"

    loop = asyncio.new_event_loop()

    async def _run():
        await mock_oauth_server_module.app.run_task(host="0.0.0.0", port=MOCK_OAUTH_PORT)

    thread = threading.Thread(target=loop.run_until_complete, args=(_run(),), daemon=True)
    thread.start()

    poll(
        lambda: requests.get(f"http://127.0.0.1:{MOCK_OAUTH_PORT}/authorize", timeout=1).status_code == 200,
        timeout=5,
        interval=0.2,
        fail_msg="Mock OAuth server did not start",
    )

    s = secrets_app_deployed["session"]
    url = secrets_app_deployed["router_url"]
    mock_oauth_domain = f"http://127.0.0.1:{MOCK_OAUTH_PORT}"

    r = s.post(
        f"{url}/secrets/test/set-mock-provider-url",
        json={
            "provider": "mock",
            "authorize_url": f"{mock_oauth_domain}/authorize",
            "token_url": f"{mock_oauth_domain}/oauth/token",
            "revoke_url": f"{mock_oauth_domain}/oauth/revoke",
            "userinfo_url": f"{mock_oauth_domain}/userinfo",
            "userinfo_field": "email",
            "redirect_uri": f"http://{ZONE_DOMAIN}/secrets/oauth/callback",
        },
        timeout=10,
    )
    assert r.status_code == 200

    yield mock_oauth_server_module

    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture(scope="module")
def oauth_demo_deployed(admin_session, router_url):
    r = admin_session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": f"file://{_OAUTH_DEMO_DIR}"},
        timeout=120,
    )
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    assert r.json().get("app_name") == "oauth-demo"
    wait_app_running(admin_session, router_url, "oauth-demo")

    yield {"session": admin_session, "router_url": router_url}
    admin_session.post(f"{router_url}/remove_app/oauth-demo", timeout=30)


# ---------------------------------------------------------------------------
# Tests — OAuth Flow (multi-account, mock provider)
# ---------------------------------------------------------------------------


@requires_docker
class TestOAuthFlow:
    """Test the OAuth flow through the oauth-demo app against a mock OAuth service.

    Verifies token retrieval, authorization redirects, and multi-account support
    for a Google-like provider.
    """

    def test_configure_mock(self, oauth_demo_deployed, mock_oauth_server):
        """Point the oauth-demo app at the mock OAuth service."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]
        mock_url = f"http://host.docker.internal:{MOCK_OAUTH_PORT}"

        r = s.post(
            f"{url}/oauth-demo/test/set-mock-url",
            json={"url": mock_url},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["mock_url"] == mock_url

    def test_no_token_returns_redirect(self, oauth_demo_deployed, mock_oauth_server):
        """When no token exists, requesting one returns an authorization redirect."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "default"},
            timeout=10,
        )
        assert r.status_code == 401
        data = r.json()
        assert "redirect_url" in data
        assert "authorize-complete" in data["redirect_url"]
        assert "provider=google" in data["redirect_url"]

    def test_authorize_first_account(self, oauth_demo_deployed, mock_oauth_server):
        """Complete authorization for the first account (alice@example.com)."""
        mock_oauth_server.add_token(
            "google",
            "https://www.googleapis.com/auth/gmail.readonly",
            "alice@example.com",
            "mock_token_alice",
        )

    def test_get_token_first_account(self, oauth_demo_deployed, mock_oauth_server):
        """After authorization, requesting a token returns it."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "alice@example.com"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["access_token"] == "mock_token_alice"

    def test_default_account_resolves(self, oauth_demo_deployed, mock_oauth_server):
        """With one account, requesting 'default' returns that account's token."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "default"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "mock_token_alice"

    def test_accounts_shows_first(self, oauth_demo_deployed, mock_oauth_server):
        """Accounts endpoint lists the connected account."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/accounts",
            json={"provider": "google"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["accounts"] == ["alice@example.com"]

    def test_authorize_second_account(self, oauth_demo_deployed, mock_oauth_server):
        """Connect a second Google account (bob@example.com)."""
        mock_oauth_server.add_token(
            "google",
            "https://www.googleapis.com/auth/gmail.readonly",
            "bob@example.com",
            "mock_token_bob",
        )

    def test_accounts_shows_both(self, oauth_demo_deployed, mock_oauth_server):
        """After connecting a second account, both are listed."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/accounts",
            json={"provider": "google"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert set(data["accounts"]) == {"alice@example.com", "bob@example.com"}

    def test_default_ambiguous_redirects(self, oauth_demo_deployed, mock_oauth_server):
        """With multiple accounts, requesting 'default' requires explicit selection."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "default"},
            timeout=10,
        )
        assert r.status_code == 401
        assert "redirect_url" in r.json()

    def test_get_token_specific_account(self, oauth_demo_deployed, mock_oauth_server):
        """Each account's token is retrievable by name."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "alice@example.com"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "mock_token_alice"

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "bob@example.com"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "mock_token_bob"

    def test_different_provider_independent(self, oauth_demo_deployed, mock_oauth_server):
        """Tokens for different providers are independent."""
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "github", "account": "default"},
            timeout=10,
        )
        assert r.status_code == 401, "GitHub should have no tokens yet"

        mock_oauth_server.add_token("github", "repo", "octocat", "mock_token_github")

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "github", "account": "octocat"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "mock_token_github"

    def test_authorize_via_redirect_flow(self, oauth_demo_deployed, mock_oauth_server):
        """Test the full redirect flow: get authorize URL, visit it, then get token."""
        mock_oauth_server.reset()
        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "new"},
            timeout=10,
        )
        assert r.status_code == 401
        authorize_url = r.json()["redirect_url"]

        local_url = authorize_url.replace("host.docker.internal", "127.0.0.1")
        local_url += "&access_token=redirected_token_123"
        r = requests.get(local_url, timeout=10)
        assert r.status_code == 200

        r = s.post(
            f"{url}/oauth-demo/test/token",
            json={"provider": "google", "account": "new"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == "redirected_token_123"

    def test_oauth_auth_code_server_side_flow(self, secrets_app_deployed, oauth_demo_deployed, mock_oauth_provider):
        """Full browser OAuth flow using Playwright — no API shortcuts.

        Unlike the other tests in this class (which call /test/token endpoints and
        inject tokens into the mock directly), this test drives a real browser through
        the entire redirect chain a human user would experience:

        1. Browser navigates to oauth-demo's server demo page.
        2. User clicks "Connect" for Google.
        3. oauth-demo calls the V2 service proxy to request a token.
        4. Router proxies to the secrets app, which has no cached token.
        5. Secrets app returns a 401 with an authorize_url pointing to the mock
           provider's /authorize page.
        6. oauth-demo redirects the browser to the authorize URL.
        7. Mock provider renders an HTML account picker (like Google's "Choose an account").
        8. User (Playwright) clicks an account (alice@example.com).
        9. Mock provider redirects to the secrets app's /oauth/callback with an
           authorization code and state.
        10. Secrets app exchanges the code for a token via POST to the mock's /oauth/token.
        11. Secrets app resolves the account identity via GET to the mock's /userinfo.
        12. Secrets app stores the token and redirects the browser back to oauth-demo.
        13. oauth-demo's server page now lists alice@example.com as a connected account.

        Setup:
        - The secrets app's Google provider URLs are overridden to point at the mock
          (auth_url -> browser-reachable 127.0.0.1, token_url/userinfo_url ->
          Docker-reachable host.docker.internal).
        - Mock client credentials are stored in the secrets app so it doesn't 503.
        - Auth cookies from the admin session are transferred to the Playwright browser
          context so the router recognizes the user.
        """

        s = oauth_demo_deployed["session"]
        url = oauth_demo_deployed["router_url"]

        # Transfer auth cookies to Playwright
        cookies = [{"name": c.name, "value": c.value, "domain": "127.0.0.1", "path": "/"} for c in s.cookies]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            context.add_cookies(cookies)
            page = context.new_page()

            # Navigate to oauth-demo server page
            page.goto(f"{url}/oauth-demo/server/", wait_until="networkidle")

            # Click "Connect" for Google — this triggers the OAuth flow
            page.click('a[href*="connect?provider=mock"]')

            # Should land on the mock provider's account picker
            page.wait_for_selector("h1", timeout=15000)
            assert "Choose an account" in page.content()

            # Click alice@example.com
            page.click('[data-testid="account-alice@example.com"]')

            # Should redirect through the callback and back to oauth-demo
            page.wait_for_url(f"**/{ZONE_DOMAIN}**/server/**", timeout=15000)

            # Verify alice is now listed as a connected account
            content = page.content()
            assert "alice@example.com" in content

            browser.close()
