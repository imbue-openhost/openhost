"""Cloud E2E tests for OpenHost self-host deployment.

Requires ``OPENHOST_DOMAIN`` env var pointing at a running instance.
See ``tests/gcp/`` or ``tests/ec2/`` for infrastructure setup scripts.
"""

import asyncio
import os
import secrets
import socket
import ssl
import string
import time

import pytest
import requests
import websockets

from compute_space.tests.utils import wait_app_removed
from compute_space.tests.utils import wait_app_running
from tests.helpers import poll_endpoint

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOMAIN = os.environ.get("OPENHOST_DOMAIN", "")
APP_DEPLOY_TIMEOUT_S = 300
# Apps live in the synced repo on the host (deployed via ansible).
TEST_APP_PATH = "/home/host/openhost/apps/test_app"
# Generate a random password per test run since instances are publicly routable.
OWNER_PASSWORD = "E2e!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


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
    """Starts unauthenticated; test_02 adds auth cookies via /setup."""
    return requests.Session()


@pytest.fixture(scope="module")
def healthy_router(router_url):
    """Block until the router responds to /health over HTTPS."""
    poll_endpoint(
        requests.Session(),
        f"{router_url}/health",
        timeout=420,
        interval=10,
        fail_msg=f"Router at {router_url} did not become healthy",
    )
    return True


# ---------------------------------------------------------------------------
# Tests -- executed in order (test_01, test_02, ...)
# ---------------------------------------------------------------------------


class TestSelfHost:
    """E2E tests for self-host deployment.

    Tests are numbered and must run in order since later tests depend on
    state created by earlier ones (owner account, deployed app, etc.).
    """

    # -- 1. Health check ---------------------------------------------------

    def test_01_health(self, router_url, healthy_router):
        """Router responds to /health with valid JSON."""
        r = requests.get(f"{router_url}/health", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "security" in data

    # -- 2. Setup (create owner) -------------------------------------------

    def test_02_setup(self, session, router_url, healthy_router):
        """First visitor to /setup becomes the owner."""
        r = session.get(f"{router_url}/setup", timeout=10)
        assert r.status_code == 200, f"/setup returned {r.status_code}: {r.text[:500]}"

        r = session.post(
            f"{router_url}/setup",
            data={
                "password": OWNER_PASSWORD,
                "confirm_password": OWNER_PASSWORD,
            },
            timeout=30,
            allow_redirects=False,
        )
        assert r.status_code == 302, f"Setup POST should redirect, got {r.status_code}: {r.text[:500]}"
        assert "/dashboard" in r.headers.get("Location", ""), (
            f"Expected redirect to /dashboard, got {r.headers.get('Location')}"
        )

        set_cookies = r.headers.get("Set-Cookie", "")
        assert "zone_auth" in set_cookies, f"Setup must set zone_auth cookie. Set-Cookie: {set_cookies[:300]}"

        session.cookies.update(r.cookies)
        r = session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    # -- 3. Dashboard access -----------------------------------------------

    def test_03_dashboard_requires_auth(self, router_url, healthy_router):
        """Unauthenticated requests to /dashboard are rejected."""
        r = requests.get(
            f"{router_url}/dashboard",
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")

    def test_03b_dashboard_with_auth(self, session, router_url):
        """Authenticated session can access /dashboard."""
        r = session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    # -- 4. Deploy test-app ------------------------------------------------

    def test_04_deploy_test_app(self, session, router_url):
        """Deploy the test-app from the apps directory on the host."""
        repo_url = f"file://{TEST_APP_PATH}"
        r = session.post(
            f"{router_url}/api/add_app",
            data={"repo_url": repo_url},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert data.get("app_name") == "test-app"

    # -- 5. Wait for app running -------------------------------------------

    def test_05_wait_app_running(self, session, router_url):
        """Poll app status until test-app reports 'running'."""
        wait_app_running(session, router_url, "test-app", timeout=APP_DEPLOY_TIMEOUT_S)

    # -- 6. Path-based routing ---------------------------------------------

    def test_06_path_routing_health(self, session, router_url):
        """test-app health check works through path-based proxy."""
        r = poll_endpoint(
            session, f"{router_url}/test-app/health", fail_msg="test-app not responding through path-based proxy"
        )
        assert r.json() == {"status": "ok"}

    def test_06b_path_routing_root(self, session, router_url):
        """GET / through proxy returns app metadata."""
        r = session.get(f"{router_url}/test-app/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"
        assert data["app_name"] == "test-app"

    def test_06c_path_routing_post(self, session, router_url):
        """POST request is proxied correctly with body."""
        r = session.post(
            f"{router_url}/test-app/submit",
            data="hello world",
            timeout=5,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["method"] == "POST"
        assert data["body"] == "hello world"
        assert data["path"] == "/submit"

    def test_06d_forwarded_headers(self, session, router_url):
        """Proxy sets X-Forwarded-* headers and strips spoofed ones."""
        r = session.get(
            f"{router_url}/test-app/echo-headers",
            headers={
                "X-Custom-Test": "test-value",
                "X-Forwarded-For": "attacker-ip",
                "X-Forwarded-Proto": "evil",
                "X-Forwarded-Host": "evil.example.com",
            },
            timeout=5,
        )
        assert r.status_code == 200
        headers = r.json()["headers"]
        # Custom headers are forwarded
        assert headers.get("X-Custom-Test") == "test-value"
        # X-Forwarded-* are set by the proxy
        assert "X-Forwarded-For" in headers
        assert "X-Forwarded-Host" in headers
        # Spoofed values are overwritten
        assert "attacker-ip" not in headers.get("X-Forwarded-For", "")
        assert headers.get("X-Forwarded-Proto") != "evil"
        assert headers.get("X-Forwarded-Host") != "evil.example.com"

    def test_06e_path_routing_404(self, session, router_url):
        """Unknown paths within the app return the app's 404."""
        r = session.get(f"{router_url}/test-app/no-such-path", timeout=5)
        assert r.status_code == 404

    def test_06f_unknown_app_404(self, router_url, session):
        """Requests to unknown app paths return 404."""
        r = session.get(f"{router_url}/no-such-app/anything", timeout=5)
        assert r.status_code == 404

    # -- 7. Subdomain routing ----------------------------------------------

    def test_07_subdomain_routing(self, session, domain):
        """test-app responds via subdomain routing (test-app.<domain>)."""
        app_url = f"https://test-app.{domain}"
        r = poll_endpoint(session, f"{app_url}/health", fail_msg=f"test-app not responding via subdomain at {app_url}")
        assert r.json() == {"status": "ok"}

    def test_07b_subdomain_root(self, session, domain):
        """Subdomain routing returns app metadata (no base_path prefix)."""
        app_url = f"https://test-app.{domain}"
        r = session.get(f"{app_url}/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"

    def test_07c_subdomain_unauth_rejected(self, domain):
        """Unauthenticated subdomain request to a non-public path is rejected."""
        app_url = f"https://test-app.{domain}"
        r = requests.get(
            f"{app_url}/echo-headers",
            allow_redirects=False,
            timeout=10,
        )
        # Non-public path without auth should be rejected
        assert r.status_code in (401, 302)

    # -- 8. App lifecycle: stop and reload ---------------------------------

    def test_08_stop_app(self, session, router_url):
        """Stop the app -- proxied requests should fail afterward."""
        r = session.post(f"{router_url}/stop_app/test-app", timeout=30)
        assert r.status_code == 200
        time.sleep(2)
        r = session.get(f"{router_url}/test-app/health", timeout=5)
        assert r.status_code in (404, 502, 503)

    def test_08b_reload_app(self, session, router_url):
        """Reload the app -- rebuilds and restarts the container."""
        r = session.post(f"{router_url}/reload_app/test-app", timeout=120)
        assert r.status_code == 200
        r = poll_endpoint(
            session,
            f"{router_url}/test-app/health",
            timeout=APP_DEPLOY_TIMEOUT_S,
            interval=5,
            fail_msg="test-app did not come back after reload",
        )
        assert r.json() == {"status": "ok"}

    # -- 9. Multiple concurrent apps ---------------------------------------

    def test_09_deploy_second_app(self, session, router_url):
        """Deploy a second instance of the test app with a different name."""
        r = session.post(
            f"{router_url}/api/add_app",
            data={"repo_url": f"file://{TEST_APP_PATH}", "app_name": "test-app-2"},
            timeout=120,
        )
        assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:500]}"
        assert r.json().get("app_name") == "test-app-2"
        wait_app_running(session, router_url, "test-app-2", timeout=APP_DEPLOY_TIMEOUT_S)

    def test_09b_both_apps_routable(self, session, router_url):
        """Both apps respond independently through path-based routing."""
        r1 = session.get(f"{router_url}/test-app/health", timeout=5)
        assert r1.status_code == 200
        assert r1.json() == {"status": "ok"}

        r2 = session.get(f"{router_url}/test-app-2/health", timeout=5)
        assert r2.status_code == 200
        assert r2.json() == {"status": "ok"}

    def test_09c_subdomain_isolation(self, session, domain):
        """Both apps respond via their own subdomains."""
        r1 = session.get(f"https://test-app.{domain}/health", timeout=5)
        assert r1.status_code == 200

        r2 = session.get(f"https://test-app-2.{domain}/health", timeout=5)
        assert r2.status_code == 200

    def test_09d_apps_list(self, session, router_url):
        """GET /api/apps returns both deployed apps."""
        r = session.get(f"{router_url}/api/apps", timeout=10)
        assert r.status_code == 200
        data = r.json()
        # /api/apps returns a dict keyed by app name
        assert "test-app" in data, f"test-app not in /api/apps response: {list(data.keys())}"
        assert "test-app-2" in data, f"test-app-2 not in /api/apps response: {list(data.keys())}"

    def test_09e_remove_second_app(self, session, router_url):
        """Remove the second app; first app still works."""
        r = session.post(f"{router_url}/remove_app/test-app-2", timeout=30)
        assert r.status_code == 202
        wait_app_removed(session, router_url, "test-app-2")

        # Second app is gone
        r = session.get(f"{router_url}/test-app-2/health", timeout=5)
        assert r.status_code == 404

        # First app still works
        r = session.get(f"{router_url}/test-app/health", timeout=5)
        assert r.status_code == 200

    # -- 10. API tokens ----------------------------------------------------

    def test_10_create_api_token(self, session, router_url):
        """Create an API token and use it to access a protected endpoint."""
        r = session.post(
            f"{router_url}/api/tokens",
            data={"name": "e2e-test-token", "expiry_hours": "1"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["name"] == "e2e-test-token"

        # Store token on the class for later tests
        TestSelfHost._api_token = data["token"]

    def test_10b_use_api_token(self, router_url):
        """API token (Bearer header, no cookies) can access /api/apps."""
        token = getattr(TestSelfHost, "_api_token", None)
        assert token, "API token not set by test_10"

        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    def test_10c_invalid_token_rejected(self, router_url):
        """A bogus Bearer token is rejected."""
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": "Bearer bogus-token-value"},
            allow_redirects=False,
            timeout=10,
        )
        # Rejected -- redirects to login or returns 401
        assert r.status_code in (302, 401)

    def test_10d_list_and_delete_token(self, session, router_url):
        """List tokens, find ours, delete it, verify it stops working."""
        r = session.get(f"{router_url}/api/tokens", timeout=10)
        assert r.status_code == 200
        tokens = r.json()
        matching = [t for t in tokens if t["name"] == "e2e-test-token"]
        assert matching, f"Token not found in list: {tokens}"
        token_id = matching[0]["id"]

        # Delete it
        r = session.delete(f"{router_url}/api/tokens/{token_id}", timeout=10)
        assert r.status_code == 200

        # Token should no longer work
        token = getattr(TestSelfHost, "_api_token", None)
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code in (302, 401)

    # -- 11. Login / logout ------------------------------------------------

    def test_11_login_bad_password(self, router_url):
        """Login with wrong credentials is rejected."""
        r = requests.post(
            f"{router_url}/login",
            data={"username": "admin", "password": "wrong-password"},
            timeout=10,
        )
        assert r.status_code == 200
        assert "Invalid password" in r.text or "invalid" in r.text.lower()

    def test_11b_login_good_password(self, router_url):
        """Login with correct credentials sets auth cookies."""
        login_session = requests.Session()
        login_session.verify = True
        r = login_session.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD},
            allow_redirects=False,
            timeout=10,
        )
        # Should redirect to dashboard
        assert r.status_code == 302
        assert "/dashboard" in r.headers.get("Location", "")
        # Auth cookies should be set
        set_cookies = r.headers.get("Set-Cookie", "")
        assert "zone_auth" in set_cookies

        # Follow redirect, verify dashboard works
        login_session.cookies.update(r.cookies)
        r = login_session.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    def test_11c_logout(self, router_url):
        """Logout clears the session."""
        # Create a fresh session, log in, then log out
        s = requests.Session()
        s.verify = True
        r = s.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD},
            timeout=10,
        )
        # Verify we're logged in
        r = s.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200

        # Logout
        r = s.post(f"{router_url}/logout", allow_redirects=False, timeout=10)
        assert r.status_code == 302

        # Dashboard should no longer be accessible
        r = s.get(f"{router_url}/dashboard", allow_redirects=False, timeout=10)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")

    # -- 12. Storage and system endpoints ----------------------------------

    def test_12_storage_status(self, session, router_url):
        """GET /api/storage-status returns valid storage info."""
        r = session.get(f"{router_url}/api/storage-status", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # Unified disk metric (no separate persistent/temporary keys)
        assert "disk" in data
        assert "total_bytes" in data["disk"]
        assert "used_bytes" in data["disk"]
        assert "free_bytes" in data["disk"]
        assert "persistent" not in data
        assert "temporary" not in data

    def test_12b_app_logs(self, session, router_url):
        """GET /app_logs/<app_name> returns log content."""
        r = session.get(f"{router_url}/app_logs/test-app", timeout=10)
        assert r.status_code == 200

    def test_12c_compute_space_logs(self, session, router_url):
        """GET /api/compute_space_logs returns router logs."""
        r = session.get(f"{router_url}/api/compute_space_logs", timeout=10)
        assert r.status_code == 200

    def test_12d_websocket_echo(self, session, domain):
        """WebSocket proxy: connect to test-app's /ws endpoint and echo a message."""
        # Extract auth cookies from the session for the WS handshake
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in session.cookies)

        async def _ws_echo():
            uri = f"wss://test-app.{domain}/ws"
            extra_headers = {"Cookie": cookie_header}
            async with websockets.connect(
                uri,
                additional_headers=extra_headers,
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send("hello from e2e test")
                response = await asyncio.wait_for(ws.recv(), timeout=5)
                assert response == "hello from e2e test"

        asyncio.run(_ws_echo())

    # -- 13. TLS certificate -----------------------------------------------

    def test_13_tls_cert(self, domain):
        """TLS cert is valid and covers the wildcard domain."""
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            socket.create_connection((domain, 443), timeout=10),
            server_hostname=domain,
        ) as sock:
            cert = sock.getpeercert()

        cert_names = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
        assert any(domain in name or name == f"*.{domain}" for name in cert_names), (
            f"Cert SANs {cert_names} do not cover {domain}"
        )

        issuer = dict(x[0] for x in cert.get("issuer", []))
        org = issuer.get("organizationName", "")
        # Google Trust Services is the primary CA (via ACME DNS-01 challenge).
        # Let's Encrypt / ISRG accepted as fallback.
        valid_issuers = ("Google Trust Services", "Let's Encrypt", "ISRG")
        assert any(name in org for name in valid_issuers), (
            f"Cert issuer '{org}' is not from a recognized CA (expected one of {valid_issuers})"
        )

    def test_13b_wildcard_tls(self, domain):
        """TLS cert covers app subdomains via wildcard."""
        ctx = ssl.create_default_context()
        app_hostname = f"test-app.{domain}"
        with ctx.wrap_socket(
            socket.create_connection((domain, 443), timeout=10),
            server_hostname=app_hostname,
        ) as sock:
            cert = sock.getpeercert()

        cert_names = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
        assert any(name == f"*.{domain}" for name in cert_names), (
            f"Cert SANs {cert_names} do not include wildcard *.{domain}"
        )

    # -- 14. Cleanup -------------------------------------------------------

    def test_14_remove_app(self, session, router_url):
        """Remove the deployed test-app."""
        r = session.post(f"{router_url}/remove_app/test-app", timeout=30)
        assert r.status_code == 202
        wait_app_removed(session, router_url, "test-app")

    def test_14b_app_gone(self, session, router_url):
        """After removal, app routes return 404."""
        r = session.get(f"{router_url}/test-app/health", timeout=5)
        assert r.status_code == 404
