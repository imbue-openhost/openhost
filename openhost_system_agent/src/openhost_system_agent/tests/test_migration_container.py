"""Container-based test: build an Ubuntu image, run all system migrations,
verify openhost starts and /health responds.

Requires podman and the --run-containers flag.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

requires_containers = pytest.mark.requires_containers

_IMAGE_NAME = "openhost-migration-test:latest"
_CONTAINER_NAME = "openhost-migration-test"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile.migration_test"


def _podman(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _exec(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _podman("exec", _CONTAINER_NAME, *args, timeout=timeout)


def _cleanup() -> None:
    _podman("rm", "-f", "-t", "0", _CONTAINER_NAME, check=False, timeout=15)
    _podman("rmi", "-f", _IMAGE_NAME, check=False, timeout=15)


def _wait_for_systemd(timeout: int = 60) -> None:
    deadline = time.time() + timeout
    state = ""
    while time.time() < deadline:
        result = _podman(
            "exec",
            _CONTAINER_NAME,
            "systemctl",
            "is-system-running",
            timeout=10,
            check=False,
        )
        state = result.stdout.strip()
        if state in ("running", "degraded"):
            return
        time.sleep(1)
    raise RuntimeError(
        f"systemd did not reach running state within {timeout}s (last: {state!r}, stderr: {result.stderr.strip()!r})"
    )


def _wait_for_health(timeout: int = 60) -> str:
    deadline = time.time() + timeout
    last_stderr = ""
    while time.time() < deadline:
        result = _podman(
            "exec",
            _CONTAINER_NAME,
            "curl",
            "-sf",
            "http://localhost:8080/health",
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        last_stderr = result.stderr.strip()
        time.sleep(2)
    raise RuntimeError(f"/health did not respond within {timeout}s (last stderr: {last_stderr!r})")


@requires_containers
class TestMigrationContainer:
    @classmethod
    def setup_class(cls) -> None:
        _cleanup()

        _podman(
            "build",
            "-t",
            _IMAGE_NAME,
            "-f",
            str(_DOCKERFILE),
            str(_REPO_ROOT),
            timeout=600,
        )

        _podman(
            "run",
            "-d",
            "--systemd=always",
            "--tmpfs=/run",
            "--tmpfs=/run/lock",
            "--cap-add=NET_ADMIN",
            "--name",
            _CONTAINER_NAME,
            _IMAGE_NAME,
            timeout=30,
        )

        _wait_for_systemd()

    @classmethod
    def teardown_class(cls) -> None:
        _cleanup()

    def test_migrations_apply(self) -> None:
        pixi = "/home/host/.pixi/bin/pixi"
        result = _exec(
            pixi,
            "run",
            "-e",
            "default",
            "python",
            "-c",
            "from openhost_system_agent.migrations.runner import apply_system_migrations; "
            "print(apply_system_migrations())",
            timeout=300,
        )
        assert result.returncode == 0, f"Migration failed:\n{result.stderr}"

    def test_openhost_service_starts(self) -> None:
        _exec("systemctl", "start", "openhost", timeout=30)
        time.sleep(2)
        result = _exec("systemctl", "is-active", "openhost", timeout=10)
        assert result.stdout.strip() == "active", f"Service not active: {result.stdout}\n{result.stderr}"

    def test_health_endpoint(self) -> None:
        body = _wait_for_health(timeout=120)
        assert '"ok"' in body or '"status"' in body
