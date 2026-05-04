"""Unit tests for the ProxyFixMiddleware wiring in ``web/start.py``.

The integration-level test
``TestContainerE2E.test_proxy_strips_spoofed_forwarded_headers`` covers
the un-wrapped path (``start_caddy=False``).  These tests cover the
wrapped path (``start_caddy=True``) without having to spin up a real
Caddy in the test environment: we drive the middleware directly with a
synthetic ASGI scope and assert that scheme / client / host get
rewritten as expected, and that ``proxy.py`` would then forward the
rewritten values onto the upstream app.

These tests cover the *behaviour* of ``ProxyFixMiddleware`` configured
with the ``mode`` / ``trusted_hops`` arguments ``main()`` uses today
(asserted via ``_make_middleware``, the local mirror of those
arguments).  They do NOT directly assert that ``main()`` actually
wraps the app — a regression that silently drops the wrap from
``start.py`` would still let these unit tests pass.  That higher-
level invariant is left to operator inspection of ``start.py`` and
to the (currently un-implemented) end-to-end path through real
Caddy + hypercorn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypercorn.middleware import ProxyFixMiddleware


async def _noop_app(_scope: Any, _receive: Any, _send: Any) -> None:
    """Inert ASGI app — gets replaced by ``_drive_middleware`` before use."""
    return None


def _make_middleware() -> ProxyFixMiddleware:
    """Build the middleware exactly the way ``main()`` does in production."""
    return ProxyFixMiddleware(_noop_app, mode="legacy", trusted_hops=1)


def _build_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    """Synthesise the relevant subset of an HTTP ASGI scope.

    Mirrors the keys that ``ProxyFixMiddleware`` reads/writes
    (``type``, ``scheme``, ``client``, ``headers``).  Anything the
    middleware doesn't touch is omitted to keep the test focused.
    """
    return {
        "type": "http",
        "scheme": "http",
        "client": ("127.0.0.1", 50001),
        "headers": headers,
    }


def _drive_middleware(middleware: ProxyFixMiddleware, scope: dict[str, Any]) -> dict[str, Any]:
    """Run the middleware against a synthetic scope, return the app-visible scope."""
    seen_scope: dict[str, Any] = {}

    async def fake_app(s: Any, receive: Any, send: Any) -> None:
        seen_scope.update(s)

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(_msg: Any) -> None:
        return None

    # ProxyFixMiddleware's typed ASGI scope is narrower than what
    # we synthesise here, but the middleware only reads keys it
    # expects so the looseness is fine for tests.  Cast via Any to
    # paper over the strict type without sprinkling ignores.
    middleware.app = fake_app
    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]
    return seen_scope


def test_proxyfix_promotes_inbound_x_forwarded_proto_to_scope_scheme() -> None:
    """``X-Forwarded-Proto: https`` inbound becomes ``scope['scheme']='https'``.

    This is the exact regression that broke peertube uploads:
    without the wrap, ``scope['scheme']`` stayed ``http`` (the loopback
    Caddy->hypercorn hop), so ``quart_request.scheme == 'http'`` and
    ``proxy.py`` forwarded ``X-Forwarded-Proto: http`` to peertube,
    which served the SPA an ``http://`` resumable-upload Location URL
    that the browser blocked as Mixed Content.
    """
    middleware = _make_middleware()
    scope = _build_scope(
        [
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-for", b"203.0.113.7"),
            (b"x-forwarded-host", b"public.example.com"),
            (b"host", b"127.0.0.1:8080"),
        ]
    )
    out = _drive_middleware(middleware, scope)
    assert out["scheme"] == "https"
    assert out["client"][0] == "203.0.113.7"
    # Host header is rewritten to the trusted forwarded value.
    host_values = [v for k, v in out["headers"] if k.lower() == b"host"]
    assert host_values == [b"public.example.com"]


def test_proxyfix_only_trusts_last_hop_with_trusted_hops_1() -> None:
    """``trusted_hops=1`` reads the last value in a comma-separated chain.

    PeerTube's mid-tier Caddy and our public Caddy both stamp
    X-Forwarded-Proto, so the header arrives as ``http, https`` (or
    just ``https``) at compute_space — but compute_space sits at the
    OUTER edge of the chain in production, so it should trust the
    immediately-upstream value.  ``trusted_hops=1`` selects the
    last comma-separated token, which is the value the
    last (i.e. closest) trusted proxy added.
    """
    middleware = _make_middleware()
    scope = _build_scope(
        [
            (b"x-forwarded-proto", b"http,https"),
            (b"host", b"127.0.0.1:8080"),
        ]
    )
    out = _drive_middleware(middleware, scope)
    # Last token wins when trusted_hops=1.
    assert out["scheme"] == "https"


def test_proxyfix_leaves_scheme_untouched_when_no_xfp_header() -> None:
    """If no inbound XFP, the scope's existing scheme is preserved."""
    middleware = _make_middleware()
    scope = _build_scope([(b"host", b"127.0.0.1:8080")])
    out = _drive_middleware(middleware, scope)
    assert out["scheme"] == "http"
    # client also stays at the bare-connection value.
    assert out["client"] == ("127.0.0.1", 50001)
