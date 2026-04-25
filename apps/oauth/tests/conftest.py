import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

OAUTH_APP_DIR = Path(__file__).resolve().parent.parent
SPEC_PATH = Path(__file__).resolve().parents[3] / "services" / "oauth" / "openapi.yaml"


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"App did not start on port {port} within {timeout}s")


_BOOT_SCRIPT = """
import asyncio, sys, os
sys.path.insert(0, os.environ["APP_DIR"])
from hypercorn.asyncio import serve
from hypercorn.config import Config
from oauth.app import app

cfg = Config()
cfg.bind = [f'127.0.0.1:{os.environ["PORT"]}']
cfg.loglevel = "WARNING"
asyncio.run(serve(app, cfg))
"""


@pytest.fixture(scope="session")
def oauth_app_url():
    db_path = str(OAUTH_APP_DIR / "tests" / "test_oauth.db")
    port = _find_free_port()
    env = {
        **os.environ,
        "OPENHOST_APP_NAME": "oauth-test",
        "OPENHOST_ZONE_DOMAIN": "test.local",
        "OPENHOST_MY_REDIRECT_DOMAIN": "test.local",
        "OPENHOST_SQLITE_MAIN": db_path,
        "APP_DIR": str(OAUTH_APP_DIR),
        "PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOT_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass
