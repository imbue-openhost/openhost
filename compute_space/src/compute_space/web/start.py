import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import hypercorn.asyncio
import hypercorn.config

from compute_space.config import load_config
from compute_space.core.caddy import start_caddy
from compute_space.core.dns import start_coredns
from compute_space.core.logging import logger
from compute_space.core.terminal import cleanup_all as cleanup_terminal_sessions
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.acquire_cert import check_if_cert_exists
from compute_space.core.updates import RESTART_EXIT_CODE
from compute_space.core.updates import initialize_shutdown_event
from compute_space.web.app import create_app


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


def main() -> None:
    # Allow group members to write files/dirs we create (files 664, dirs 775).
    os.umask(0o002)

    config = load_config()
    config.make_all_dirs()
    children: list[subprocess.Popen[bytes]] = []

    if config.coredns_enabled:
        if not config.public_ip:
            raise RuntimeError("Public IP must be set in config to use CoreDNS")
        children.append(
            start_coredns(
                config.zone_domain, config.public_ip, config.coredns_corefile_path, config.coredns_zonefile_path
            )
        )

    if config.tls_enabled:
        if not check_if_cert_exists(config.tls_cert_path, config.tls_key_path):
            if not config.coredns_enabled:
                raise RuntimeError("CoreDNS must be enabled to acquire TLS cert via DNS-01 challenge")
            if not config.acme_account_key_path:
                raise RuntimeError("ACME account key path must be set in config to acquire TLS cert")
            if not config.acquire_tls_cert_if_missing:
                raise RuntimeError("TLS cert not found and acquire_tls_cert_if_missing is False")
            asyncio.run(
                acquire_tls_cert(
                    domain=config.zone_domain,
                    cert_path=config.tls_cert_path,
                    key_path=config.tls_key_path,
                    acme_account_key_path=Path(config.acme_account_key_path),
                    coredns_zonefile_path=config.coredns_zonefile_path,
                    acme_email=config.acme_email,
                )
            )

    # Caddy reverse proxy. mainly for TLS termination, but also some other features
    if config.start_caddy:
        children.append(
            start_caddy(
                config.caddyfile_path, config.tls_enabled, config.tls_cert_path, config.tls_key_path, config.port
            )
        )
    else:
        if config.tls_enabled:
            raise RuntimeError("TLS is enabled but start_caddy is False. Caddy is required for TLS termination.")

    # Main web server
    app = create_app(config)

    hypercorn_config = hypercorn.config.Config()
    hypercorn_config.bind = [f"{config.host}:{config.port}"]
    hypercorn_config.graceful_timeout = 3
    hypercorn_config.shutdown_timeout = 5

    logger.info("running hypercorn serve")
    restart_requested = asyncio.run(_serve(app, hypercorn_config))
    logger.info(f"hypercorn serve returned, restart_requested={restart_requested}")

    _terminate_children(children)

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
