import os
import subprocess

import pytest


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
