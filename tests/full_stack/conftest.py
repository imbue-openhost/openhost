"""Shared fixtures for full-stack integration tests.

These are module-scoped: each test file that uses them gets its own router
process, admin session, and deployed apps.
"""

import os

import pytest
import requests

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import DefaultConfig
from compute_space.testing import managed_router
from compute_space.testing import poll
from compute_space.testing import wait_app_running

_APPS_DIR = str(OPENHOST_PROJECT_DIR / "apps")
_SECRETS_DIR = os.path.join(_APPS_DIR, "secrets")

ROUTER_PORT = 28080
OWNER_PASSWORD = "routerpass123"
ZONE_DOMAIN = f"testzone.localhost:{ROUTER_PORT}"
MOCK_OAUTH_PORT = 29199

requires_docker = pytest.mark.requires_docker


@pytest.fixture(scope="module")
def config(tmp_path_factory):
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
    with managed_router(config) as proc:
        yield proc


@pytest.fixture(scope="module")
def router_url(config):
    return f"http://{config.host}:{config.port}"


@pytest.fixture(scope="module")
def admin_session(router_process, router_url):
    s = requests.Session()
    r = s.post(
        f"{router_url}/setup",
        data={"password": OWNER_PASSWORD, "confirm_password": OWNER_PASSWORD},
        allow_redirects=False,
    )
    assert r.status_code == 302, f"Router setup failed: {r.status_code}"
    r = s.get(f"{router_url}/dashboard")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="module")
def secrets_app_deployed(admin_session, router_url):
    r = admin_session.post(
        f"{router_url}/api/add_app",
        data={"repo_url": f"file://{_SECRETS_DIR}"},
        timeout=120,
    )
    assert r.status_code == 200, f"add_app failed: {r.status_code}: {r.text[:300]}"
    assert r.json().get("app_name") == "secrets"
    wait_app_running(admin_session, router_url, "secrets")

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

    yield {"session": admin_session, "router_url": router_url}
    admin_session.post(f"{router_url}/remove_app/secrets", timeout=30)
