"""Root conftest – opt-in flags for heavy integration tests.

By default only lightweight tests run (no external runtimes needed).
Pass flags to opt in to heavier suites:

    uv run pytest                      # local-only tests
    uv run pytest --run-containers         # + Podman integration tests
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
        "--run-containers",
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
    """Gate for --run-containers tests: is podman *usable* on this host?

    Runs ``podman info`` (not just ``--version``) because the
    ``requires_containers`` tests need a working rootless namespace, not
    just the binary on PATH.  This is intentionally a heavier probe
    than the production ``compute_space.core.containers.podman_available``
    which only verifies binary presence — those two probes answer
    different questions (can I build/run containers? vs should I
    surface the 'runtime missing' banner?) and deliberately diverge.
    """
    try:
        r = subprocess.run(["podman", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # OSError covers odd failure modes (EPERM on the binary, fd
        # exhaustion, etc.) that would otherwise crash pytest
        # collection rather than gracefully skip --run-containers tests.
        return False


def _pebble_available():
    return shutil.which("pebble") is not None


def _coredns_available():
    return shutil.which("coredns") is not None


# ---------------------------------------------------------------------------
# Auto-skip logic
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    run_containers = config.getoption("--run-containers")
    run_tls = config.getoption("--run-tls")

    if run_containers and not _podman_available():
        raise RuntimeError("--run-containers flag passed but podman does not seem to be available")

    if run_tls and not _pebble_available():
        raise RuntimeError("--run-tls flag passed but pebble is not in PATH")
    if run_tls and not _coredns_available():
        raise RuntimeError("--run-tls flag passed but coredns is not in PATH")

    for item in items:
        if "requires_containers" in item.keywords:
            if not run_containers:
                item.add_marker(pytest.mark.skip(reason="needs --run-containers flag to run"))

        if "requires_tls" in item.keywords:
            if not run_tls:
                item.add_marker(pytest.mark.skip(reason="needs --run-tls flag to run"))
