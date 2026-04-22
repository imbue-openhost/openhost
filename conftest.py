"""Root conftest – opt-in flags for heavy integration tests.

By default only lightweight tests run (no external runtimes needed).
Pass flags to opt in to heavier suites:

    uv run pytest                      # local-only tests
    uv run pytest --run-podman         # + Podman integration tests
    uv run pytest --run-tls            # + TLS cert tests
"""

import shutil
import subprocess

import pytest

# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--run-podman",
        action="store_true",
        default=False,
        help="Run tests that require a working rootless podman setup.",
    )
    parser.addoption(
        "--run-tls",
        action="store_true",
        default=False,
        help="Run TLS cert tests that require pebble and coredns.",
    )


# ---------------------------------------------------------------------------
# Tool availability checks
# ---------------------------------------------------------------------------


def _podman_available():
    try:
        r = subprocess.run(["podman", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # OSError covers odd failure modes (EPERM on the binary, fd
        # exhaustion, etc.) that would otherwise crash pytest
        # collection rather than gracefully skip --run-podman tests.
        return False


def _pebble_available():
    return shutil.which("pebble") is not None


def _coredns_available():
    return shutil.which("coredns") is not None


# ---------------------------------------------------------------------------
# Auto-skip logic
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    run_podman = config.getoption("--run-podman")
    run_tls = config.getoption("--run-tls")

    if run_podman and not _podman_available():
        raise RuntimeError("--run-podman flag passed but podman does not seem to be available")

    if run_tls and not _pebble_available():
        raise RuntimeError("--run-tls flag passed but pebble is not in PATH")
    if run_tls and not _coredns_available():
        raise RuntimeError("--run-tls flag passed but coredns is not in PATH")

    for item in items:
        if "requires_podman" in item.keywords:
            if not run_podman:
                item.add_marker(pytest.mark.skip(reason="needs --run-podman flag to run"))

        if "requires_tls" in item.keywords:
            if not run_tls:
                item.add_marker(pytest.mark.skip(reason="needs --run-tls flag to run"))
