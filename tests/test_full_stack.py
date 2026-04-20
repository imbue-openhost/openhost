"""
Full-stack integration tests for the OpenHost router.

Starts the router directly (no QEMU VMs, no multiuser_provider), deploys apps
via rootless Podman, and exercises routing, auth, WebSockets, API tokens, app
lifecycle, and system endpoints over HTTP.

Prerequisites:
    - Rootless podman configured (see ansible/tasks/podman.yml)
    - *.localhost resolves to 127.0.0.1 (RFC 6761, default on most Linux systems)

Run:
    pytest tests/test_full_stack.py -v -s --run-podman --timeout=600
"""

import asyncio
import os
import time

import pytest
import requests
import websockets

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import DefaultConfig
from compute_space.testing import managed_router
from compute_space.testing import poll
from compute_space.testing import wait_app_running

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_APPS_DIR = str(OPENHOST_PROJECT_DIR / "apps")
_TEST_APP_DIR = os.path.join(_APPS_DIR, "test_app")

ROUTER_PORT = 28080
OWNER_PASSWORD = "routerpass123"
ZONE_DOMAIN = "testzone.localhost"

requires_podman = pytest.mark.requires_podman


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    """Create a DefaultConfig with temp dirs, suitable for running the router directly."""
    tmpdir = tmp_path_factory.mktemp("fullstack_router")
    cfg = DefaultConfig(
        host="127.0.0.1",
        port=ROUTER_PORT,
        data_root_dir=str(tmpdir),
        apps_dir_override=_APPS_DIR,
        port_range_start=29000,
        port_range_end=29099,
        zone_domain=ZONE_DOMAIN,
        tls_enabled=False,
        start_caddy=False,
    )
    cfg.make_all_dirs()
    return cfg


@pytest.fixture(scope="module")
def router_process(config):
    """Start the router as a subprocess, wait for /health, tear down after."""
    with managed_router(config) as proc:
        yield proc


@pytest.fixture(scope="module")
def router_url(config):
    """Base URL for the router."""
    return f"http://{config.host}:{config.port}"


@pytest.fixture(scope="module")
def admin_session(router_process, router_url):
    """A requests.Session authenticated as the router owner via /setup."""
    s = requests.Session()
    r = s.post(
        f"{router_url}/setup",
        data={
            "password": OWNER_PASSWORD,
            "confirm_password": OWNER_PASSWORD,
        },
        allow_redirects=False,
    )
    assert r.status_code == 302, f"Router setup failed: {r.status_code}"
    assert "/dashboard" in r.headers.get("Location", ""), (
        f"Setup should redirect to /dashboard, got {r.headers.get('Location')}"
    )

    # Verify auth cookies were set
    set_cookies = r.headers.get("Set-Cookie", "")
    assert "zone_auth" in set_cookies, f"Setup redirect must set zone_auth cookie. Set-Cookie: {set_cookies[:200]}"

    # Follow the redirect to confirm dashboard access
    r = s.get(f"{router_url}/dashboard")
    assert r.status_code == 200, (
        f"Dashboard not accessible after setup (status {r.status_code}). Cookies: {[c.name for c in s.cookies]}"
    )
    assert "Deployed Apps" in r.text
    return s


def app_url(app_name):
    """Build a subdomain URL for an app: http://{app}.{zone}.localhost:{port}."""
    return f"http://{app_name}.{ZONE_DOMAIN}:{ROUTER_PORT}"


@pytest.fixture(scope="module")
def test_app_deployed(admin_session, router_url):
    """Deploy the container-based test-app and wait for it to be running."""
    repo_url = f"file://{_TEST_APP_DIR}"
    r = admin_session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": repo_url},
        timeout=120,
    )
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    assert r.json().get("app_name") == "test-app"

    wait_app_running(admin_session, router_url, "test-app")

    return {
        "session": admin_session,
        "router_url": router_url,
    }


# ---------------------------------------------------------------------------
# Tests — Router Core
# ---------------------------------------------------------------------------


@requires_podman
class TestRouter:
    def test_router_health(self, router_process, router_url):
        r = requests.get(f"{router_url}/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_setup_auto_login(self, admin_session, router_url):
        """After setup, auth cookies give immediate access to /dashboard."""
        cookie_names = [c.name for c in admin_session.cookies]
        assert "zone_auth" in cookie_names
        assert "zone_refresh" in cookie_names

        r = admin_session.get(f"{router_url}/dashboard")
        assert r.status_code == 200
        assert "Deployed Apps" in r.text

    def test_dashboard_requires_auth(self, router_process, router_url):
        """Unauthenticated requests to dashboard redirect to login."""
        r = requests.get(f"{router_url}/dashboard", allow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")


# ---------------------------------------------------------------------------
# Tests — App Deployment & Path Routing
# ---------------------------------------------------------------------------


@requires_podman
class TestTestAppPathRouting:
    """Test test-app via path-based routing (/test-app/...)."""

    def test_deployed(self, test_app_deployed):
        assert test_app_deployed["router_url"]

    def test_health(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]

        def _check():
            try:
                r = s.get(f"{url}/test-app/health", timeout=5)
                return r.status_code == 200
            except requests.ConnectionError:
                return False

        poll(_check, timeout=30, interval=1, fail_msg="test-app not responding through proxy")
        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.json() == {"status": "ok"}

    def test_root_returns_metadata(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/test-app/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"
        assert data["app_name"] == "test-app"

    def test_post_proxied(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.post(f"{url}/test-app/submit", data="hello world", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["method"] == "POST"
        assert data["body"] == "hello world"
        assert data["path"] == "/submit"

    def test_forwarded_headers(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(
            f"{url}/test-app/echo-headers",
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
        assert headers.get("X-Custom-Test") == "test-value"
        assert "X-Forwarded-For" in headers
        assert "attacker-ip" not in headers.get("X-Forwarded-For", "")
        assert headers.get("X-Forwarded-Proto") != "evil"
        assert headers.get("X-Forwarded-Host") != "evil.example.com"

    def test_unknown_path_404(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/test-app/no-such-path", timeout=5)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tests — App Subdomain Routing
# ---------------------------------------------------------------------------


@requires_podman
class TestTestAppSubdomainRouting:
    """Test test-app via subdomain routing (test-app.testzone.localhost:port)."""

    def test_subdomain_health(self, test_app_deployed):
        s = test_app_deployed["session"]
        sub_url = app_url("test-app")

        def _check():
            try:
                r = s.get(f"{sub_url}/health", timeout=5)
                return r.status_code == 200
            except requests.ConnectionError:
                return False

        poll(_check, timeout=30, interval=1, fail_msg="test-app not responding via subdomain")
        r = s.get(f"{sub_url}/health", timeout=5)
        assert r.json() == {"status": "ok"}

    def test_subdomain_root(self, test_app_deployed):
        s = test_app_deployed["session"]
        sub_url = app_url("test-app")
        r = s.get(f"{sub_url}/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "test-app"

    def test_subdomain_unauth_rejected(self, test_app_deployed):
        sub_url = app_url("test-app")
        r = requests.get(f"{sub_url}/echo-headers", allow_redirects=False)
        assert r.status_code in (401, 302)


# ---------------------------------------------------------------------------
# Tests — App Lifecycle
# ---------------------------------------------------------------------------


@requires_podman
class TestAppLifecycle:
    """Test app stop and reload through the router."""

    def test_stop_app(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.post(f"{url}/stop_app/test-app", timeout=30)
        assert r.status_code == 200

        time.sleep(2)
        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.status_code in (404, 502, 503)

    def test_reload_app(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.post(f"{url}/reload_app/test-app", timeout=120)
        assert r.status_code == 200

        def _check():
            try:
                r = s.get(f"{url}/test-app/health", timeout=5)
                return r.status_code == 200
            except requests.ConnectionError:
                return False

        poll(_check, timeout=300, interval=5, fail_msg="test-app did not come back after reload")
        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Tests — Multiple Apps
# ---------------------------------------------------------------------------


@requires_podman
class TestMultipleApps:
    """Test deploying multiple apps concurrently."""

    def test_deploy_second_app(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.post(
            f"{url}/api/add_app",
            data={"repo_url": f"file://{_TEST_APP_DIR}", "app_name": "test-app-2"},
            timeout=120,
        )
        assert r.status_code == 200
        assert r.json().get("app_name") == "test-app-2"

        wait_app_running(s, url, "test-app-2")

    def test_both_apps_respond(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]

        r1 = s.get(f"{url}/test-app/health", timeout=5)
        assert r1.status_code == 200
        assert r1.json() == {"status": "ok"}

        r2 = s.get(f"{url}/test-app-2/health", timeout=5)
        assert r2.status_code == 200
        assert r2.json() == {"status": "ok"}

    def test_apps_list(self, test_app_deployed):
        """GET /api/apps returns both deployed apps."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/api/apps", timeout=10)
        assert r.status_code == 200
        data = r.json()
        # /api/apps returns a dict keyed by app name
        assert "test-app" in data, f"test-app not in /api/apps response: {list(data.keys())}"
        assert "test-app-2" in data, f"test-app-2 not in /api/apps response: {list(data.keys())}"

    def test_subdomain_isolation(self, test_app_deployed):
        s = test_app_deployed["session"]
        r1 = s.get(f"{app_url('test-app')}/health", timeout=5)
        assert r1.status_code == 200

        r2 = s.get(f"{app_url('test-app-2')}/health", timeout=5)
        assert r2.status_code == 200

    def test_remove_second_app(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.post(f"{url}/remove_app/test-app-2", timeout=30)
        assert r.status_code == 200

        r = s.get(f"{url}/test-app-2/health", timeout=5)
        assert r.status_code == 404

        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tests — API Tokens
# ---------------------------------------------------------------------------


@requires_podman
class TestAPITokens:
    """Test API token create, use, and delete."""

    def test_create_token(self, admin_session, router_url):
        s = admin_session
        r = s.post(
            f"{router_url}/api/tokens",
            data={"name": "full-stack-test-token", "expiry_hours": "1"},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["name"] == "full-stack-test-token"
        TestAPITokens._token = data["token"]

    def test_use_token(self, router_url):
        token = getattr(TestAPITokens, "_token", None)
        assert token, "Token not set by test_create_token"
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert r.status_code == 200

    def test_invalid_token_rejected(self, router_url):
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": "Bearer bogus-token"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code in (302, 401)

    def test_delete_token(self, admin_session, router_url):
        s = admin_session
        r = s.get(f"{router_url}/api/tokens", timeout=10)
        assert r.status_code == 200
        tokens = r.json()
        matching = [t for t in tokens if t["name"] == "full-stack-test-token"]
        assert matching
        token_id = matching[0]["id"]

        r = s.delete(f"{router_url}/api/tokens/{token_id}", timeout=10)
        assert r.status_code == 200

        # Deleted token should no longer work
        token = getattr(TestAPITokens, "_token", None)
        r = requests.get(
            f"{router_url}/api/apps",
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code in (302, 401)


# ---------------------------------------------------------------------------
# Tests — Login / Logout
# ---------------------------------------------------------------------------


@requires_podman
class TestLoginLogout:
    """Test login and logout flows."""

    def test_login_bad_password(self, admin_session, router_url):
        r = requests.post(
            f"{router_url}/login",
            data={"username": "admin", "password": "wrong-password"},
            timeout=10,
        )
        assert r.status_code == 200
        assert "Invalid password" in r.text or "invalid" in r.text.lower()

    def test_login_good_password(self, admin_session, router_url):
        s = requests.Session()
        r = s.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD},
            allow_redirects=False,
            timeout=10,
        )
        assert r.status_code == 302
        assert "/dashboard" in r.headers.get("Location", "")
        set_cookies = r.headers.get("Set-Cookie", "")
        assert "zone_auth" in set_cookies

    def test_logout(self, admin_session, router_url):
        s = requests.Session()

        # Log in
        r = s.post(
            f"{router_url}/login",
            data={"username": "admin", "password": OWNER_PASSWORD},
            timeout=10,
        )
        r = s.get(f"{router_url}/dashboard", timeout=10)
        assert r.status_code == 200

        # Log out
        r = s.get(f"{router_url}/logout", allow_redirects=False, timeout=10)
        assert r.status_code == 302

        # Dashboard should no longer be accessible
        r = s.get(f"{router_url}/dashboard", allow_redirects=False, timeout=10)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")


# ---------------------------------------------------------------------------
# Tests — Storage & System
# ---------------------------------------------------------------------------


@requires_podman
class TestStorageAndSystem:
    def test_storage_status(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/api/storage-status", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_app_logs(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/app_logs/test-app", timeout=10)
        assert r.status_code == 200

    def test_compute_space_logs(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/api/compute_space_logs", timeout=10)
        assert r.status_code == 200

    def test_security_audit(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/api/security-audit", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Tests — SSH Toggle
# ---------------------------------------------------------------------------


@requires_podman
class TestSSHToggle:
    def test_ssh_status(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/api/ssh-status", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data or "ssh_enabled" in data

    def test_toggle_ssh_off_and_on(self, admin_session, router_url):
        r = admin_session.post(f"{router_url}/toggle-ssh", timeout=10)
        assert r.status_code == 200

        r = admin_session.post(f"{router_url}/toggle-ssh", timeout=10)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tests — WebSocket Proxy
# ---------------------------------------------------------------------------


@requires_podman
class TestWebSocketProxy:
    def test_ws_echo_path_routing(self, test_app_deployed):
        """WebSocket echo via path-based routing (/test-app/ws)."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
        ws_url = url.replace("http://", "ws://")
        ws_uri = f"{ws_url}/test-app/ws"

        async def _ws_echo():
            async with websockets.connect(
                ws_uri,
                additional_headers={"Cookie": cookie_header},
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send("hello from full-stack test")
                response = await asyncio.wait_for(ws.recv(), timeout=5)
                assert response == "hello from full-stack test"

        asyncio.run(_ws_echo())

    def test_ws_echo_subdomain_routing(self, test_app_deployed):
        """WebSocket echo via subdomain routing (test-app.testzone.localhost)."""
        s = test_app_deployed["session"]
        sub_url = app_url("test-app")
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
        ws_url = sub_url.replace("http://", "ws://")
        ws_uri = f"{ws_url}/ws"

        async def _ws_echo():
            async with websockets.connect(
                ws_uri,
                additional_headers={"Cookie": cookie_header},
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send("subdomain ws test")
                response = await asyncio.wait_for(ws.recv(), timeout=5)
                assert response == "subdomain ws test"

        asyncio.run(_ws_echo())


# ---------------------------------------------------------------------------
# Tests — App Rename
# ---------------------------------------------------------------------------


@requires_podman
class TestAppRename:
    def test_rename_app(self, test_app_deployed):
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]

        # Verify app is running
        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.status_code == 200

        # Rename it
        r = s.post(
            f"{url}/rename_app/test-app",
            data={"new_name": "renamed-app"},
            timeout=30,
        )
        assert r.status_code == 200

        # New name should be routable
        def _check():
            try:
                r = s.get(f"{url}/renamed-app/health", timeout=5)
                return r.status_code == 200
            except requests.ConnectionError:
                return False

        poll(_check, timeout=30, interval=2, fail_msg="renamed-app not responding after rename")

        # Old name should be gone
        r = s.get(f"{url}/test-app/health", timeout=5)
        assert r.status_code == 404

        # Rename back for subsequent tests
        r = s.post(
            f"{url}/rename_app/renamed-app",
            data={"new_name": "test-app"},
            timeout=30,
        )
        assert r.status_code == 200

        def _check_original():
            try:
                r = s.get(f"{url}/test-app/health", timeout=5)
                return r.status_code == 200
            except requests.ConnectionError:
                return False

        poll(
            _check_original,
            timeout=30,
            interval=2,
            fail_msg="test-app not responding after rename back",
        )


# ---------------------------------------------------------------------------
# Tests — Cleanup
# ---------------------------------------------------------------------------


@requires_podman
class TestCleanup:
    """Final cleanup: remove the deployed test-app."""

    def test_remove_test_app(self, test_app_deployed, admin_session, router_url):
        r = admin_session.post(f"{router_url}/remove_app/test-app", timeout=30)
        assert r.status_code == 200

    def test_test_app_gone(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/test-app/health", timeout=5)
        assert r.status_code == 404
