import asyncio
import os
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import hypercorn.asyncio
import hypercorn.config

from compute_space.config import Config
from compute_space.config import load_config
from compute_space.config import set_active_config
from compute_space.core.auth.keys import load_keys
from compute_space.core.caddy import CaddyProcess
from compute_space.core.caddy import start_caddy
from compute_space.core.dns import start_coredns
from compute_space.core.logging import logger
from compute_space.core.logging import setup_file_logging
from compute_space.core.terminal import cleanup_all as cleanup_terminal_sessions
from compute_space.core.tls.provision import provision_cert
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.tls.renewal import start_renewal_thread
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db import init_db
from compute_space.web.app import create_app
from compute_space.web.setup_app import create_setup_app


def _terminate_children(children: list[subprocess.Popen[bytes]]) -> None:
    for proc in children:
        if proc.poll() is None:
            logger.info(f"Terminating child process {proc.pid}")
            proc.terminate()
    for proc in children:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning(f"Child process {proc.pid} did not exit, killing")
            proc.kill()


def _bootstrap(config: Config) -> None:
    """One-time process-wide initialization shared by the setup and full apps."""
    set_active_config(config)
    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")
    load_keys(config.keys_dir)
    init_db(config.db_path)


def _owner_exists(config: Config) -> bool:
    db = sqlite3.connect(config.db_path)
    try:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        db.close()


def _ensure_tls_cert(config: Config) -> None:
    """Make sure a usable cert+key pair is on disk before Caddy starts, acquiring or renewing as configured."""
    status = get_cert_status(config.tls_cert_path, config.tls_key_path)
    if status == CertStatus.OK:
        logger.info(f"Using existing TLS cert from {config.tls_cert_path}")
        return
    if not config.coredns_enabled or not config.acquire_tls_cert_if_missing:
        # A cert nearing expiry still works, so don't block startup over it.
        if status == CertStatus.EXPIRING_SOON:
            logger.warning("TLS cert expires soon but automatic cert acquisition is not enabled; cannot renew")
            return
        if not config.coredns_enabled:
            raise RuntimeError("CoreDNS must be enabled to acquire TLS cert via DNS-01 challenge")
        raise RuntimeError(f"TLS cert is {status.value} and acquire_tls_cert_if_missing is False")
    if status == CertStatus.EXPIRING_SOON:
        # The existing cert is still valid, so a failed renewal shouldn't block
        # startup — the background renewal loop will keep retrying.
        try:
            provision_cert(config)
        except Exception:
            logger.exception("TLS cert renewal failed; serving the existing cert and retrying in the background")
    else:
        provision_cert(config)


def main() -> None:
    # Allow group members to write files/dirs we create (files 664, dirs 775).
    os.umask(0o002)

    config = load_config()
    config.make_all_dirs()
    _bootstrap(config)
    children: list[subprocess.Popen[bytes]] = []

    if config.coredns_enabled:
        if not config.public_ip:
            raise RuntimeError("Public IP must be set in config to use CoreDNS")
        children.append(
            start_coredns(
                config.zone_domain,
                config.public_ip,
                config.coredns_corefile_path,
                config.coredns_zonefile_path,
                bind_ip_override=config.coredns_bind_ip,
            )
        )

    if config.tls_enabled:
        _ensure_tls_cert(config)

    # Caddy reverse proxy. mainly for TLS termination, but also some other features
    caddy: CaddyProcess | None = None
    if config.start_caddy:
        caddy = start_caddy(
            config.caddyfile_path, config.tls_enabled, config.tls_cert_path, config.tls_key_path, config.port
        )
        if config.tls_enabled and config.coredns_enabled and config.acquire_tls_cert_if_missing:
            start_renewal_thread(config, caddy.restart)
    else:
        if config.tls_enabled:
            raise RuntimeError("TLS is enabled but start_caddy is False. Caddy is required for TLS termination.")

    def _all_children() -> list[subprocess.Popen[bytes]]:
        # Read caddy.proc at shutdown time: restart() may have replaced it.
        return children + ([caddy.proc] if caddy is not None else [])

    hypercorn_config = hypercorn.config.Config()
    # Bind the primary address (127.0.0.1 in production) plus the container
    # gateway (10.200.0.1) so podman containers can reach the router via
    # host.containers.internal.  No need for 0.0.0.0 — Caddy handles
    # external traffic on 80/443 and proxies to us on loopback.
    binds = [f"{config.host}:{config.port}"]
    container_gateway = "10.200.0.1"
    if config.host != "0.0.0.0" and config.host != container_gateway:
        # Only add the gateway bind if the interface actually exists (it won't
        # in dev mode or CI where openhost0 hasn't been created by ansible).
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind((container_gateway, 0))
            probe.close()
            binds.append(f"{container_gateway}:{config.port}")
        except OSError:
            pass
    hypercorn_config.bind = binds
    hypercorn_config.graceful_timeout = 3
    hypercorn_config.shutdown_timeout = 5

    # First-boot setup: serve a minimal app until the owner is provisioned.  The setup
    # handler triggers shutdown via trigger_restart(); we then proceed to the full app
    if not _owner_exists(config):
        logger.info("No owner row found; serving setup-only app")
        setup_completed = asyncio.run(_serve(create_setup_app(config), hypercorn_config))
        if not setup_completed:
            logger.info("Setup interrupted by signal; exiting")
            _terminate_children(_all_children())
            time.sleep(0.1)
            os._exit(0)

    # Main web server
    app = create_app(config)
    logger.info("running hypercorn serve")
    restart_requested = asyncio.run(_serve(app, hypercorn_config))
    logger.info(f"hypercorn serve returned, restart_requested={restart_requested}")

    _terminate_children(_all_children())

    if restart_requested:
        logger.info(f"Calling os._exit({RESTART_EXIT_CODE})")
        time.sleep(0.1)
        os._exit(RESTART_EXIT_CODE)

    logger.info("Calling os._exit(0)")
    time.sleep(0.1)
    os._exit(0)


async def _serve(app: Any, hypercorn_config: hypercorn.config.Config) -> bool:
    """Run hypercorn with a shutdown trigger wired to the update system.

    Returns True if shutdown was triggered by a restart request (not a signal).
    """
    shutdown_event = asyncio.Event()
    initialize_shutdown_event(shutdown_event)

    signal_received = False
    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        nonlocal signal_received
        signal_received = True
        logger.info("Signal received, shutting down gracefully")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def shutdown_trigger() -> None:
        await shutdown_event.wait()
        logger.info("shutdown trigger unblocked")
        cleanup_terminal_sessions()

    await hypercorn.asyncio.serve(app, hypercorn_config, shutdown_trigger=shutdown_trigger)

    return shutdown_event.is_set() and not signal_received


if __name__ == "__main__":
    main()
