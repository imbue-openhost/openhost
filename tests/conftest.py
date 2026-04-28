import os
import subprocess
import time
import uuid

import pytest
import requests

_test_run_id = uuid.uuid4().hex[:6]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--use-existing-instance",
        metavar="NAME",
        help="Run e2e tests against an existing oh instance (hostname or alias)",
    )


def pytest_configure(config: pytest.Config) -> None:
    name = config.getoption("--use-existing-instance", default=None)
    if not name:
        return

    def oh_run(*args: str) -> str:
        result = subprocess.run(["oh", *args], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.exit(f"oh {' '.join(args)} failed: {result.stderr.strip()}", returncode=1)
        return result.stdout.strip()

    token = oh_run("--instance", name, "instance", "token")

    # Resolve alias → hostname by parsing `oh instance list`.
    hostname = None
    for line in oh_run("instance", "list").splitlines():
        parts = line.split()
        if not parts:
            continue
        h = parts[0]
        if h == name or f"alias: {name}" in line:
            hostname = h
            break

    if not hostname:
        pytest.exit(f"Could not resolve instance '{name}' from 'oh instance list'", returncode=1)

    os.environ["OPENHOST_DOMAIN"] = hostname
    os.environ["OPENHOST_TOKEN"] = token
    os.environ["E2E_RUN_ID"] = _test_run_id

    _sync_instance(hostname, token)


def _sync_instance(hostname: str, token: str) -> None:
    """Push current repo state to the instance and restart it."""
    result = subprocess.run(
        ["git", "log", "@{u}..HEAD", "--oneline"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.exit("Could not check for unpushed commits (no upstream tracking branch?)", returncode=1)
    if result.stdout.strip():
        pytest.exit(
            f"Unpushed commits on current branch:\n{result.stdout.strip()}\n"
            "Push before running against an existing instance.",
            returncode=1,
        )

    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    remote_url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()

    url = f"https://{hostname}"
    headers = {"Authorization": f"Bearer {token}"}

    # Save current remote state so we can restore after tests.
    resp = requests.get(f"{url}/api/settings/get_remote", headers=headers, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        os.environ["_E2E_PRIOR_REMOTE_URL"] = data.get("url") or ""
        os.environ["_E2E_PRIOR_REMOTE_REF"] = data.get("ref") or ""

    # Point the instance at this exact commit.
    print(f"\nSyncing instance {hostname} to {commit[:12]}...")
    resp = requests.post(
        f"{url}/api/settings/set_remote",
        json={"url": f"{remote_url}@{commit}"},
        headers=headers,
        timeout=120,
    )
    if resp.status_code != 200 or not resp.json().get("ok"):
        pytest.exit(f"set_remote failed: {resp.text}", returncode=1)

    _restart_and_wait(url, headers)


def _restart_and_wait(url: str, headers: dict[str, str]) -> None:
    print("Restarting instance...")
    requests.post(f"{url}/api/settings/restart_compute_space", headers=headers, timeout=10)

    print("Waiting for instance to come back up...", end="", flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            r = requests.get(f"{url}/health", timeout=5)
            if r.status_code == 200:
                print(" ok")
                return
        except requests.ConnectionError:
            pass
        print(".", end="", flush=True)
    pytest.exit(f"Instance at {url} did not come back within 5 minutes", returncode=1)


def pytest_unconfigure(config: pytest.Config) -> None:
    name = config.getoption("--use-existing-instance", default=None)
    if not name:
        return

    hostname = os.environ.get("OPENHOST_DOMAIN", "")
    token = os.environ.get("OPENHOST_TOKEN", "")
    if not hostname or not token:
        return

    url = f"https://{hostname}"
    headers = {"Authorization": f"Bearer {token}"}

    _cleanup_test_apps(url, headers)
    _restore_prior_remote(url, headers)


def _cleanup_test_apps(url: str, headers: dict[str, str]) -> None:
    """Remove any apps deployed by this test run (best effort)."""
    prefix = f"t{_test_run_id}-"
    try:
        resp = requests.get(f"{url}/api/apps", headers=headers, timeout=10)
        if resp.status_code != 200:
            return
        apps = resp.json()
        for app_name in apps:
            if app_name.startswith(prefix):
                print(f"Cleaning up test app '{app_name}'...")
                requests.post(f"{url}/remove_app/{app_name}", headers=headers, timeout=30)
    except Exception as e:
        print(f"Warning: app cleanup failed: {e}")


def _restore_prior_remote(url: str, headers: dict[str, str]) -> None:
    """Restore the instance to its prior remote/ref."""
    prior_url = os.environ.get("_E2E_PRIOR_REMOTE_URL", "")
    prior_ref = os.environ.get("_E2E_PRIOR_REMOTE_REF", "")
    if not prior_url or not prior_ref:
        return

    try:
        print(f"Restoring instance to {prior_url}@{prior_ref[:12]}...")
        resp = requests.post(
            f"{url}/api/settings/set_remote",
            json={"url": f"{prior_url}@{prior_ref}"},
            headers=headers,
            timeout=120,
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            print(f"Warning: restore failed: {resp.text}")
            return
        _restart_and_wait(url, headers)
    except Exception as e:
        print(f"Warning: restore failed: {e}")
