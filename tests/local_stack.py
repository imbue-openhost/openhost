"""Test-side helpers for a full local OpenHost stack (router + podman apps).

Config building lives in compute_space.local_stack; this module adds the requests-based
setup/deploy flows used by tests.  All owner requests must go through the zone domain
(not 127.0.0.1) so the session cookie — scoped to the zone domain — is accepted by the
client and sent to app subdomains.
"""

import subprocess

import attr
import requests

from compute_space.config import Config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.tests.utils import poll
from compute_space.tests.utils import wait_app_running

OWNER_PASSWORD = "localstackpass123"


@attr.s(auto_attribs=True, frozen=True)
class LocalStack:
    config: Config
    owner_password: str = OWNER_PASSWORD
    # app names deployed via deploy_app, so remove_deployed_app_containers can clean up
    deployed_app_names: list[str] = attr.Factory(list)

    @property
    def router_url(self) -> str:
        return f"http://{self.config.zone_domain}"

    def app_url(self, app_name: str) -> str:
        return f"http://{app_name}.{self.config.zone_domain}"

    def remove_deployed_app_containers(self) -> None:
        """Remove app containers after the router is gone.

        App containers run with ``--restart=unless-stopped`` and are not children of the
        router process, so killing the router leaves them running.  Call this in fixture
        teardown to avoid leaking containers across test runs.
        """
        for app_name in self.deployed_app_names:
            subprocess.run(["podman", "rm", "-f", f"openhost-{app_name}"], capture_output=True, timeout=60)


def complete_setup(stack: LocalStack, timeout: float = 60) -> requests.Session:
    """Provision the owner via /setup and return an authenticated session.

    /setup responds 200 with the session cookie, then restarts the router process
    into the full app — so we poll /dashboard until the full app answers with our
    cookie.
    """
    session = requests.Session()
    r = session.post(
        f"{stack.router_url}/setup",
        data={"password": stack.owner_password, "confirm_password": stack.owner_password},
        timeout=30,
    )
    assert r.status_code == 200, f"/setup failed: {r.status_code}: {r.text[:300]}"
    cookie_names = [c.name for c in session.cookies]
    assert SESSION_COOKIE_NAME in cookie_names, f"setup did not set {SESSION_COOKIE_NAME} cookie, got {cookie_names}"

    def _dashboard_up() -> bool:
        try:
            return session.get(f"{stack.router_url}/dashboard", timeout=2).status_code == 200
        except requests.ConnectionError:
            return False

    poll(_dashboard_up, timeout=timeout, interval=0.5, fail_msg="full app did not come up after /setup")
    return session


def deploy_app(
    session: requests.Session,
    stack: LocalStack,
    repo_url: str,
    app_name: str | None = None,
    grant_manifest_permissions: bool = False,
    timeout: float = 300,
) -> str:
    """Deploy an app via /api/add_app and wait until it is running.  Returns the app_id."""
    payload: dict[str, str | bool] = {"repo_url": repo_url}
    if app_name is not None:
        payload["app_name"] = app_name
    if grant_manifest_permissions:
        payload["grant_permissions_v2"] = True
    r = session.post(f"{stack.router_url}/api/add_app", json=payload, timeout=120)
    assert r.status_code == 200, f"add_app({repo_url}) failed: {r.status_code}: {r.text[:500]}"
    body = r.json()
    stack.deployed_app_names.append(body["app_name"])
    wait_app_running(session, stack.router_url, body["app_name"], timeout=timeout)
    return str(body["app_id"])
