import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import hypercorn.asyncio
import hypercorn.config
from hypercorn.middleware import ProxyFixMiddleware

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
    quart_app = create_app(config)

    # Wrap the ASGI app in hypercorn's ProxyFixMiddleware ONLY when
    # we expect a trusted upstream proxy (``start_caddy=True``,
    # i.e. our public Caddy reverse_proxy is in front of us).
    # Without the wrap, ``quart_request.scheme`` is always ``http``
    # — the scheme of the loopback Caddy->hypercorn hop — even when
    # the original public request was HTTPS.  Downstream
    # (``proxy.py``) sets ``X-Forwarded-Proto = quart_request.scheme``
    # on the upstream request to each app, so the un-wrapped path
    # silently rewrites the public ``https`` to ``http`` for every
    # proxied request, which breaks any app that constructs absolute
    # URLs from ``X-Forwarded-Proto`` (peertube's @uploadx/core
    # resumable upload library is the canonical example: it builds
    # a ``http://...`` resumable-upload Location header on a HTTPS
    # page, and the browser blocks it as Mixed Content).
    #
    # Trust model and ``trusted_hops=1``: the middleware copies the
    # SINGLE most-recent ``X-Forwarded-*`` value in each header
    # onto the ASGI scope (``scope['scheme']``, ``scope['client']``,
    # ``host`` header), i.e. whatever the immediately-upstream
    # proxy added.  This trust is unconditional — the middleware
    # has no IP-allowlist mechanism and trusts the connection peer
    # implicitly.  We therefore wrap ONLY when Caddy is in front of
    # us, because Caddy's ``reverse_proxy`` default behaviour is to
    # OVERWRITE any client-supplied ``X-Forwarded-*`` headers with
    # values derived from the actual TCP connection — so a hostile
    # client cannot smuggle forged values past Caddy.  When
    # ``start_caddy=False`` (development, tests, or operators who
    # have an alternative trusted proxy), we leave the app
    # un-wrapped and ``quart_request.scheme`` remains the connection
    # scheme; operators putting their own proxy in front are
    # expected to either run with TLS termination at hypercorn or
    # set ``start_caddy=True`` to re-enable the wrap.
    #
    # KNOWN CAVEAT: hypercorn binds to ``config.host`` which defaults
    # to ``0.0.0.0``, so even with ``start_caddy=True`` an attacker
    # with direct network access to the bind port can bypass Caddy
    # and supply arbitrary ``X-Forwarded-*`` values that ProxyFix
    # will then trust.  In the OpenHost reference deployment, the
    # OS-level firewall and the (lack of) NAT publishing for port
    # 8080 keep the hypercorn socket reachable only from the same
    # host as Caddy.  Tightening this — by binding hypercorn to
    # ``127.0.0.1`` whenever ``start_caddy=True``, or switching to a
    # UNIX socket — is a hardening followup worth doing but out of
    # scope for the immediate fix.
    #
    # ``mode="legacy"`` reads the conventional ``X-Forwarded-For``
    # / ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` triplet rather
    # than the newer RFC 7239 ``Forwarded`` header.  Caddy stamps
    # the legacy headers by default, and most of the apps we
    # proxy to are configured to read the legacy headers, so the
    # legacy mode keeps the wire format consistent end-to-end.
    asgi_app: Any
    if config.start_caddy:
        asgi_app = ProxyFixMiddleware(quart_app, mode="legacy", trusted_hops=1)
    else:
        asgi_app = quart_app

    hypercorn_config = hypercorn.config.Config()
    hypercorn_config.bind = [f"{config.host}:{config.port}"]
    hypercorn_config.graceful_timeout = 3
    hypercorn_config.shutdown_timeout = 5

    logger.info("running hypercorn serve")
    restart_requested = asyncio.run(_serve(asgi_app, hypercorn_config))
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
