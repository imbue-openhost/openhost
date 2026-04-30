"""Shared test utilities for process and network helpers.

Importable from both top-level tests/ and compute_space/tests/ since
the compute_space package is installed.
"""

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import IO
from typing import Any

import pytest
import requests

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space.config import Config


def kill_tree(proc: subprocess.Popen[Any], sig: int = signal.SIGTERM) -> None:
    """Kill a process and its children via process group."""
    try:
        os.killpg(proc.pid, sig)
    except (PermissionError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def port_connectable(host: str, port: int, timeout: float = 1) -> bool:
    """Check if a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def poll(fn: Any, timeout: float, interval: float = 5, fail_msg: str = "Polling timed out") -> Any:
    """Call *fn* repeatedly until it returns a truthy value or *timeout* expires."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as e:
            last_exc = e
        time.sleep(interval)
    extra = f" (last exception: {last_exc})" if last_exc else ""
    pytest.fail(f"{fail_msg}{extra}")


def wait_app_running(session: requests.Session, router_url: str, app_name: str, timeout: float = 300) -> None:
    """Poll ``/api/app_status/<app>`` until the app reports *running*."""

    def _check() -> bool:
        r = session.get(f"{router_url}/api/app_status/{app_name}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data["status"] == "error":
                pytest.fail(f"{app_name} deploy failed: {data.get('error')}")
            return bool(data["status"] == "running")
        return False

    poll(_check, timeout=timeout, interval=5, fail_msg=f"{app_name} did not reach 'running' state")


def wait_app_removed(session: requests.Session, router_url: str, app_name: str, timeout: float = 120) -> None:
    """Poll ``/api/app_status/<app>`` until the row is gone (404).

    /remove_app returns 202 and runs teardown in a background thread;
    tests asserting on filesystem / container state after a remove
    must wait for the row to disappear (the row is the last step).
    """

    def _check() -> bool:
        r = session.get(f"{router_url}/api/app_status/{app_name}", timeout=10)
        if r.status_code == 404:
            return True
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "error":
                pytest.fail(f"{app_name} removal failed: {data.get('error')}")
        return False

    poll(_check, timeout=timeout, interval=2, fail_msg=f"{app_name} was not removed within {timeout}s")


def router_cmd() -> list[str]:
    """Return the command list to launch the router as a subprocess.

    Uses ``sys.executable`` rather than shelling out through pixi: the
    test runner is itself launched via ``pixi run -e dev pytest``, so
    ``sys.executable`` already points at the dev env's Python and
    ``compute_space`` is importable as the editable package install.
    """
    return [sys.executable, "-m", "compute_space"]


# ---------------------------------------------------------------------------
# Router process context manager
# ---------------------------------------------------------------------------

# Global so atexit can clean up if pytest crashes before teardown runs.
_router_proc: subprocess.Popen[bytes] | None = None


def _kill_router() -> None:
    if _router_proc and _router_proc.poll() is None:
        kill_tree(_router_proc)


atexit.register(_kill_router)


@contextmanager
def managed_router(config: Config, startup_timeout: int = 30) -> Generator[subprocess.Popen[bytes], None, None]:
    """Start a router subprocess, wait for /health, yield it, then tear down.

    Registers an atexit handler so the process is killed even if pytest crashes.
    Logs stdout/stderr to ``{config.temporary_data_dir}/router.log``.
    """
    global _router_proc

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((config.host, config.port)) == 0:
            raise RuntimeError(f"Port {config.port} already in use — kill the stale process")

    config_path = os.path.join(config.temporary_data_dir, "config.toml")
    config.to_toml(config_path)
    env = os.environ.copy()
    env["OPENHOST_CONFIG"] = config_path
    env["SECRET_KEY"] = "test-secret-key"

    log_path = os.path.join(config.temporary_data_dir, "router.log")
    log_file: IO[str] = open(log_path, "w")
    base_url = f"http://{config.host}:{config.port}"

    try:
        proc = subprocess.Popen(
            router_cmd(),
            cwd=str(COMPUTE_SPACE_PACKAGE_DIR),
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        _router_proc = proc
    except Exception:
        log_file.close()
        raise

    # Wait for /health
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=1)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.3)
    else:
        kill_tree(proc)
        proc.wait()
        log_file.close()
        with open(log_path) as f:
            log_content = f.read()
        raise RuntimeError(f"Router failed to start.\nlog: {log_content}")

    try:
        yield proc
    finally:
        try:
            kill_tree(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.wait()
        finally:
            _router_proc = None
            log_file.close()
