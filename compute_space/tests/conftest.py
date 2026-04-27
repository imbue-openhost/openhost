import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.db.migrations import _schema_path
from compute_space.testing import kill_tree
from compute_space.testing import managed_router

from .helpers import COMPUTE_SPACE_PACKAGE_DIR
from .helpers import router_cmd

ROUTER_PORT = 18080
OWNER_PASSWORD = "testpass123"


class _FakeApp:
    """Minimal Quart-like stand-in exposing just ``.config['DB_PATH']``.

    Used by tests that call ``compute_space.db.connection.init_db``
    (which reads ``app.config['DB_PATH']``) without setting up a real
    Quart app.  Lives in conftest so every test module that needs one
    can import a single shared implementation.
    """

    def __init__(self, db_path: str) -> None:
        self.config = {"DB_PATH": db_path}


def _make_test_config(tmp_path: Path, **overrides: Any) -> Config:
    """Create a DefaultConfig with temp dirs under tmp_path. Returns the Config object."""
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        apps_dir_override=str(OPENHOST_PROJECT_DIR / "apps"),
        port_range_start=overrides.pop("port_range_start", 19000),
        port_range_end=overrides.pop("port_range_end", 19099),
        zone_domain=overrides.pop("zone_domain", "testzone.local"),
        tls_enabled=overrides.pop("tls_enabled", False),
        start_caddy=overrides.pop("start_caddy", False),
        **overrides,
    )
    cfg.make_all_dirs()
    return cfg


def _make_test_env(config_path: str) -> dict[str, str]:
    """Build an env dict for launching a router subprocess."""
    env = os.environ.copy()
    env["OPENHOST_CONFIG"] = config_path
    env["SECRET_KEY"] = "test-secret-key"
    return env


def _make_config_and_env(tmp_path: Path, **overrides: Any) -> tuple[Config, dict[str, str]]:
    """Create a test config + env dict for launching a router subprocess."""
    config = _make_test_config(tmp_path, **overrides)
    config_path = str(tmp_path / "config.toml")
    config.to_toml(config_path)
    return config, _make_test_env(config_path)


def _start_router_process(base_url: str, env: dict[str, str], startup_timeout: int = 30) -> subprocess.Popen[bytes]:
    """Start a router subprocess, wait for /health, return the Popen object."""
    proc = subprocess.Popen(
        router_cmd(),
        cwd=COMPUTE_SPACE_PACKAGE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return proc
        except (requests.ConnectionError, requests.ReadTimeout):
            pass
        time.sleep(0.3)
    out, err = proc.communicate(timeout=5)
    os.killpg(proc.pid, signal.SIGKILL)
    raise RuntimeError(f"Router failed to start.\nstdout: {out.decode()}\nstderr: {err.decode()}")


def _stop_router_process(proc: subprocess.Popen[Any]) -> None:
    """Stop a router subprocess."""
    kill_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite database loaded with the real production schema.

    FK enforcement is left off (sqlite default) so individual tests don't
    have to insert an apps row for every consumer/provider name they
    reference — these tests are about service / permission semantics, not
    referential integrity.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(_schema_path()) as f:
        conn.executescript(f.read())
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """Create a test config with temp dirs, write to TOML, return the Config object."""
    tmpdir = tmp_path_factory.mktemp("router")
    return _make_test_config(tmpdir, port=ROUTER_PORT)


@pytest.fixture(scope="module")
def router_process(config: Config) -> subprocess.Popen[bytes]:
    """Start the router as a subprocess, wait for /health, tear down after."""
    with managed_router(config) as proc:
        yield proc


@pytest.fixture(scope="module")
def admin_session(router_process: subprocess.Popen[bytes], config: Config) -> requests.Session:
    """A requests.Session authenticated as the router owner."""
    base_url = f"http://{config.host}:{config.port}"
    s = requests.Session()
    r = s.post(
        f"{base_url}/setup",
        data={
            "password": OWNER_PASSWORD,
            "confirm_password": OWNER_PASSWORD,
        },
    )
    assert r.status_code == 200, f"Router setup failed: {r.status_code}"
    return s
