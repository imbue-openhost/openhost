"""E2E test for the full browser OAuth authorization code flow.

Deploys the oauth app, oauth-demo app, and mock-oauth-provider onto a
running OpenHost instance, then drives a real browser through the entire
redirect chain using Playwright.

Requires:
    OPENHOST_DOMAIN  — hostname of the target instance
    OPENHOST_TOKEN   — bearer token for API auth (from ``oh instance token``)

Run via the helper script::

    python tests/run_e2e.py tests/test_e2e_oauth.py
    python tests/run_e2e.py -k oauth
"""

import os
import secrets
import string

import pytest
import requests
from playwright.sync_api import sync_playwright

from compute_space.testing import wait_app_running
from tests.helpers import poll_endpoint

DOMAIN = os.environ.get("OPENHOST_DOMAIN", "")
TOKEN = os.environ.get("OPENHOST_TOKEN", "")
APP_DEPLOY_TIMEOUT_S = 300

OAUTH_APP_PATH = "/home/host/openhost/apps/oauth"
OAUTH_DEMO_PATH = "/home/host/openhost/apps/oauth_demo"
MOCK_PROVIDER_PATH = "/home/host/openhost/apps/mock_oauth_provider"


@pytest.fixture(scope="module")
def domain():
    if not DOMAIN:
        pytest.skip("OPENHOST_DOMAIN not set")
    return DOMAIN


@pytest.fixture(scope="module")
def router_url(domain):
    return f"https://{domain}"


@pytest.fixture(scope="module")
def session(router_url):
    """Authenticated session — uses bearer token if available, otherwise /setup."""
    s = requests.Session()
    if TOKEN:
        s.headers["Authorization"] = f"Bearer {TOKEN}"
        r = s.get(f"{router_url}/api/apps", timeout=10)
        assert r.status_code == 200, f"Token auth failed: {r.status_code}"
    else:
        password = "E2e!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        poll_endpoint(
            s,
            f"{router_url}/health",
            timeout=420,
            interval=10,
            fail_msg=f"Router at {router_url} did not become healthy",
        )
        r = s.post(
            f"{router_url}/setup",
            data={"password": password, "confirm_password": password},
            allow_redirects=False,
            timeout=30,
        )
        assert r.status_code == 302, f"Setup failed: {r.status_code}"
        s.cookies.update(r.cookies)
    return s


def _is_app_running(session, router_url, app_name):
    try:
        r = session.get(f"{router_url}/api/app_status/{app_name}", timeout=10)
        return r.status_code == 200 and r.json().get("status") == "running"
    except Exception:
        return False


def _deploy_app(session, router_url, app_name, repo_path):
    if _is_app_running(session, router_url, app_name):
        return
    r = session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": f"file://{repo_path}"},
        timeout=120,
    )
    assert r.status_code == 200, f"deploy {app_name} failed: {r.status_code}: {r.text[:500]}"
    wait_app_running(session, router_url, app_name, timeout=APP_DEPLOY_TIMEOUT_S)


@pytest.fixture(scope="module")
def oauth_app(session, router_url):
    _deploy_app(session, router_url, "oauth", OAUTH_APP_PATH)
    yield
    if not _is_app_running(session, router_url, "oauth"):
        return
    session.post(f"{router_url}/remove_app/oauth", timeout=30)


@pytest.fixture(scope="module")
def oauth_demo_app(session, router_url):
    _deploy_app(session, router_url, "oauth-demo", OAUTH_DEMO_PATH)
    yield
    if not _is_app_running(session, router_url, "oauth-demo"):
        return
    session.post(f"{router_url}/remove_app/oauth-demo", timeout=30)


@pytest.fixture(scope="module")
def mock_provider_app(session, router_url):
    _deploy_app(session, router_url, "mock-oauth-provider", MOCK_PROVIDER_PATH)
    poll_endpoint(
        session,
        f"{router_url}/mock-oauth-provider/health",
        timeout=60,
        interval=3,
        fail_msg="mock-oauth-provider health check failed",
    )
    yield
    if not _is_app_running(session, router_url, "mock-oauth-provider"):
        return
    session.post(f"{router_url}/remove_app/mock-oauth-provider", timeout=30)


@pytest.fixture(scope="module")
def configured_mock_provider(session, router_url, domain, oauth_app, oauth_demo_app, mock_provider_app):
    """Configure the oauth app and oauth-demo to use the deployed mock provider."""
    mock_base = f"https://{domain}/mock-oauth-provider"

    # Reset mock provider state
    r = session.post(f"{mock_base}/reset", timeout=10)
    assert r.status_code == 200

    # Point the oauth app's "mock" provider at the deployed mock-oauth-provider
    r = session.post(
        f"{router_url}/oauth/test/set-mock-provider-url",
        json={
            "provider": "mock",
            "authorize_url": f"{mock_base}/authorize",
            "token_url": f"{mock_base}/oauth/token",
            "revoke_url": f"{mock_base}/oauth/revoke",
            "userinfo_url": f"{mock_base}/userinfo",
            "userinfo_field": "email",
            "redirect_uri": f"https://{domain}/_services_v2/oauth/callback",
        },
        timeout=10,
    )
    assert r.status_code == 200, f"set-mock-provider-url failed: {r.status_code}: {r.text[:300]}"

    # Point oauth-demo at the mock provider API for /emails
    r = session.post(
        f"{router_url}/oauth-demo/test/set-mock-url",
        json={"provider_api_url": mock_base},
        timeout=10,
    )
    assert r.status_code == 200, f"set-mock-url failed: {r.status_code}: {r.text[:300]}"

    return mock_base


class TestOAuthFlow:
    """Full browser OAuth flow against a real OpenHost instance."""

    def test_oauth_browser_flow(self, session, router_url, domain, configured_mock_provider):
        """Drive a real browser through the entire OAuth redirect chain.

        1. Navigate to oauth-demo's server page
        2. Click "Connect" for mock provider → account picker
        3. Pick alice@example.com → redirect chain → back to oauth-demo
        4. Verify alice is connected and permissions granted
        5. Click "Emails" → verify mock emails display
        """
        # Transfer auth to Playwright. All URLs are on the same domain so
        # extra_http_headers works for bearer tokens; cookies work for session auth.
        extra_headers = {}
        cookies = []
        if TOKEN:
            extra_headers["Authorization"] = f"Bearer {TOKEN}"
        else:
            cookies = [
                {"name": c.name, "value": c.value, "domain": f".{domain}", "path": "/"} for c in session.cookies
            ]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(ignore_https_errors=True, extra_http_headers=extra_headers)
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            page.goto(f"https://{domain}/oauth-demo/server/", wait_until="networkidle")

            # Click "Connect" for mock provider
            page.click('a[href*="connect?provider=mock"]')

            # Should land on the mock provider's account picker
            page.wait_for_selector("h1", timeout=30000)
            assert "Choose an account" in page.content()

            # Click alice@example.com
            page.click('[data-testid="account-alice@example.com"]')

            # Wait for redirect chain to settle back at oauth-demo
            page.wait_for_load_state("networkidle", timeout=30000)
            assert "/server/" in page.url, f"unexpected landing URL: {page.url}"

            # Verify alice is listed as a connected account
            content = page.content()
            assert "alice@example.com" in content

            # Verify oauth-demo was granted the expected permission
            r = session.get(f"{router_url}/api/permissions_v2", params={"app": "oauth-demo"}, timeout=10)
            assert r.status_code == 200
            perms = r.json()
            mock_perms = [perm for perm in perms if perm["grant"].get("provider") == "mock"]
            assert len(mock_perms) == 1
            assert mock_perms[0]["grant"] == {
                "provider": "mock",
                "scopes": ["mock.emails"],
                "account": "alice@example.com",
            }
            assert mock_perms[0]["scope"] == "app"
            assert mock_perms[0]["consumer_app"] == "oauth-demo"

            # Click "Emails" for alice — uses the token to hit the mock API
            page.click('a[href*="emails?account=alice"]')
            page.wait_for_selector(".email", timeout=15000)

            content = page.content()
            assert "Welcome to the mock" in content
            assert "Your invoice is ready" in content

            browser.close()
