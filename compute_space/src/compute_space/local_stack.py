"""Helpers for running a local stack: an HTTP-only router on a ``*.localhost`` zone.

``*.localhost`` resolves to loopback on Linux and macOS without any DNS setup, so this works
in browsers, curl, and tests with no real domain.  Used by tests/local_stack.py and
scripts/run_local_stack.py.
"""

import os

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.config import DefaultConfig


def make_local_stack_config(
    data_root_dir: str,
    port: int,
    zone_name: str,
    port_range_start: int = 9000,
    port_range_end: int = 9999,
    default_apps: list[str] | None = None,
) -> Config:
    """Config for a loopback-only, HTTP-only router suitable for local dev and tests.

    ``default_apps=None`` keeps DefaultConfig's standard set (deployed at /setup completion);
    pass ``[]`` to deploy nothing.
    """
    config: Config = DefaultConfig(
        zone_domain=f"{zone_name}.localhost:{port}",
        host="127.0.0.1",
        port=port,
        data_root_dir=data_root_dir,
        apps_dir_override=str(OPENHOST_PROJECT_DIR / "apps"),
        port_range_start=port_range_start,
        port_range_end=port_range_end,
        tls_enabled=False,
        start_caddy=False,
        claim_token_required=False,
    )
    if default_apps is not None:
        config = config.evolve(default_apps=default_apps)
    config.make_all_dirs()
    return config


def make_router_env(config_path: str) -> dict[str, str]:
    """Subprocess env for launching the router against a generated config."""
    return {**os.environ, "OPENHOST_ROUTER_CONFIG": config_path}
