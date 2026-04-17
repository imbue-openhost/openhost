"""Root conftest – opt-in flags for heavy integration tests.

By default only lightweight tests run (no external runtimes needed).
Pass flags to opt in to heavier suites:

    uv run pytest                      # local-only tests
    uv run pytest --run-docker         # + Docker tests
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
        "--run-docker",
        action="store_true",
        default=False,
        help="Run tests that require a running Docker daemon.",
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


def _docker_available():
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _pebble_available():
    return shutil.which("pebble") is not None


def _coredns_available():
    return shutil.which("coredns") is not None


# ---------------------------------------------------------------------------
# Auto-skip logic
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    run_docker = config.getoption("--run-docker")
    run_tls = config.getoption("--run-tls")

    if run_docker and not _docker_available():
        raise RuntimeError("--run-docker flag passed but Docker does not seem to be available")

    if run_tls and not _pebble_available():
        raise RuntimeError("--run-tls flag passed but pebble is not in PATH")
    if run_tls and not _coredns_available():
        raise RuntimeError("--run-tls flag passed but coredns is not in PATH")

    for item in items:
        if "requires_docker" in item.keywords:
            if not run_docker:
                item.add_marker(pytest.mark.skip(reason="needs --run-docker flag to run"))

        if "requires_tls" in item.keywords:
            if not run_tls:
                item.add_marker(pytest.mark.skip(reason="needs --run-tls flag to run"))
