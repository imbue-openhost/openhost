"""
Full-stack integration tests for the OpenHost router.

Starts the router directly (no QEMU VMs, no multiuser_provider), deploys apps
via rootless Podman, and exercises routing, auth, WebSockets, API tokens, app
lifecycle, and system endpoints over HTTP.

Prerequisites:
    - Rootless podman configured (see ansible/tasks/podman.yml)
    - *.localhost resolves to 127.0.0.1 (RFC 6761, default on most Linux systems)

Run:
    pytest tests/test_full_stack.py -v -s --run-containers --timeout=600
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
_SECRETS_DIR = os.path.join(_APPS_DIR, "secrets")

SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"

ROUTER_PORT = 28080
OWNER_PASSWORD = "routerpass123"
ZONE_DOMAIN = f"testzone.localhost:{ROUTER_PORT}"

requires_containers = pytest.mark.requires_containers


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
    """Build a subdomain URL for an app: http://{app}.{zone_domain}/."""
    return f"http://{app_name}.{ZONE_DOMAIN}"


@pytest.fixture(scope="module")
def test_app_deployed(admin_session, router_url):
    """Deploy the container-based test-app and wait for it to be running."""
    repo_url = f"file://{_TEST_APP_DIR}"
    r = admin_session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": repo_url, "grant_permissions_v2": "true"},
        timeout=120,
    )
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    assert r.json().get("app_name") == "test-app"

    wait_app_running(admin_session, router_url, "test-app")

    return {
        "session": admin_session,
        "router_url": router_url,
    }


@pytest.fixture(scope="module")
def secrets_app_deployed(admin_session, router_url):
    """Deploy the secrets app and store a test secret."""
    r = admin_session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": f"file://{_SECRETS_DIR}"},
        timeout=120,
    )
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    assert r.json().get("app_name") == "secrets"

    wait_app_running(admin_session, router_url, "secrets")

    # Store a test secret via the secrets app API
    def _store():
        try:
            r = admin_session.post(
                f"{router_url}/secrets/api/secrets",
                json={"key": "TEST_SECRET", "value": "s3cret_value_42"},
                timeout=10,
            )
            return r.status_code == 200
        except requests.ConnectionError:
            return False

    poll(_store, timeout=30, interval=2, fail_msg="Could not store test secret")

    yield {
        "session": admin_session,
        "router_url": router_url,
    }

    admin_session.post(f"{router_url}/remove_app/secrets", timeout=30)


# ---------------------------------------------------------------------------
# Tests — Router Core
# ---------------------------------------------------------------------------


@requires_containers
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


@requires_containers
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


@requires_containers
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


@requires_containers
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


@requires_containers
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


@requires_containers
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


@requires_containers
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
        r = s.post(f"{router_url}/logout", allow_redirects=False, timeout=10)
        assert r.status_code == 302

        # Dashboard should no longer be accessible
        r = s.get(f"{router_url}/dashboard", allow_redirects=False, timeout=10)
        assert r.status_code == 302
        assert "/login" in r.headers.get("Location", "")


# ---------------------------------------------------------------------------
# Tests — Storage & System
# ---------------------------------------------------------------------------


@requires_containers
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


@requires_containers
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


@requires_containers
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


@requires_containers
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
# Tests — V2 Services (cross-app service proxy)
# ---------------------------------------------------------------------------


@requires_containers
class TestServicesV2:
    """Test V2 cross-app services: secrets app provides, test-app consumes.

    The test-app manifest declares [[permissions_v2]] requesting
    TEST_SECRET from the secrets service. It is deployed with
    grant_permissions_v2=true so the permission is granted at install time.
    """

    def test_service_registered(self, secrets_app_deployed, admin_session, router_url):
        """Deploying the secrets app registers its V2 service provider."""
        r = admin_session.get(f"{router_url}/api/services_v2", timeout=10)
        assert r.status_code == 200
        services = r.json()
        providers = [s for s in services if s["service_url"] == SECRETS_SERVICE_URL]
        assert len(providers) == 1
        assert providers[0]["app_name"] == "secrets"
        assert providers[0]["service_version"] == "0.1.0"

    def test_install_time_grant_works(self, secrets_app_deployed, test_app_deployed):
        """test-app was deployed with grant_permissions_v2=true, so it can
        fetch the secret immediately without an explicit grant call."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/test-app/fetch-secret/TEST_SECRET", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["secrets"]["TEST_SECRET"] == "s3cret_value_42"

    def test_revoke_then_denied_with_grant_url(self, secrets_app_deployed, test_app_deployed):
        """After revoking, 403 response includes a valid grant_url that loads the approval page."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]

        r = s.post(
            f"{url}/api/permissions_v2/revoke",
            json={
                "app": "test-app",
                "service_url": SECRETS_SERVICE_URL,
                "grant": {"key": "TEST_SECRET"},
            },
            timeout=10,
        )
        assert r.status_code == 200

        r = s.get(f"{url}/test-app/fetch-secret/TEST_SECRET", timeout=15)
        assert r.status_code == 403
        data = r.json()
        assert data["error"] == "permission_required"
        assert "required_grant" in data
        grant_url = data["required_grant"].get("grant_url")
        assert grant_url, "403 response should include a grant_url"

        r = s.get(grant_url, timeout=10)
        assert r.status_code == 200, f"grant_url returned {r.status_code}"

    def test_regrant_via_grant_url(self, secrets_app_deployed, test_app_deployed):
        """The grant_url from a 403 leads to a working grant flow."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]

        r = s.get(f"{url}/test-app/fetch-secret/TEST_SECRET", timeout=15)
        assert r.status_code == 403
        data = r.json()
        grant = data["required_grant"]
        grant_url = grant.get("grant_url")
        assert grant_url

        r = s.post(
            f"{url}/api/permissions_v2/grant-global-scoped",
            json={
                "app": "test-app",
                "service_url": SECRETS_SERVICE_URL,
                "grant": grant["grant_payload"],
            },
            timeout=10,
        )
        assert r.status_code == 200

        r = s.get(f"{url}/test-app/fetch-secret/TEST_SECRET", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["secrets"]["TEST_SECRET"] == "s3cret_value_42"

    def test_ungranted_key_still_denied(self, secrets_app_deployed, test_app_deployed):
        """A key not covered by any grant is denied."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(f"{url}/test-app/fetch-secret/OTHER_SECRET", timeout=15)
        assert r.status_code == 403

    def test_version_mismatch_rejected(self, secrets_app_deployed, test_app_deployed):
        """Requesting an incompatible version returns 503."""
        s = test_app_deployed["session"]
        url = test_app_deployed["router_url"]
        r = s.get(
            f"{url}/test-app/fetch-secret/TEST_SECRET",
            params={"version": ">=99.0.0"},
            timeout=15,
        )
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# Tests — Cleanup
# ---------------------------------------------------------------------------


@requires_containers
class TestCleanup:
    """Final cleanup: remove the deployed test-app."""

    def test_remove_test_app(self, test_app_deployed, admin_session, router_url):
        r = admin_session.post(f"{router_url}/remove_app/test-app", timeout=30)
        assert r.status_code == 200

    def test_test_app_gone(self, admin_session, router_url):
        r = admin_session.get(f"{router_url}/test-app/health", timeout=5)
        assert r.status_code == 404
