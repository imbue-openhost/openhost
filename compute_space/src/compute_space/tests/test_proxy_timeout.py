"""Tests for the proxy's timeout behavior.

Regression coverage for the intermittent ``504 App timed out`` that broke
Matrix ``/sync`` long-polls: the router proxied app requests with a single 30s
timeout, so a long-poll that held ~30s with no events was cut off and the
client saw a disconnection.  The fix keeps a short *connect* timeout (fail fast
for dead apps) but a generous *read* timeout so long-polls aren't killed.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from litestar import Request
from litestar.response.base import ASGIResponse
from litestar.response.streaming import ASGIStreamingResponse

from compute_space.web.helpers.proxy import _DEFAULT_CONNECT_TIMEOUT
from compute_space.web.helpers.proxy import _build_httpx_timeout
from compute_space.web.helpers.proxy import proxy_http_request

# ---------------------------------------------------------------------------
# Unit tests: _build_httpx_timeout shaping
# ---------------------------------------------------------------------------


def test_default_timeout_has_no_read_timeout() -> None:
    """The default must not impose a read timeout (long-polls must survive)."""
    t = _build_httpx_timeout(None)
    assert t.read is None
    assert t.connect == _DEFAULT_CONNECT_TIMEOUT


def test_scalar_timeout_sets_connect_but_leaves_read_open() -> None:
    """A bare number sets the connect (fail-fast) timeout; read stays open."""
    t = _build_httpx_timeout(5)
    assert t.connect == 5
    assert t.write == 5
    assert t.pool == 5
    # Critically, the read timeout is NOT set to the scalar -- long-polls survive.
    assert t.read is None


def test_explicit_httpx_timeout_passed_through() -> None:
    """An httpx.Timeout is honored as-is (full control for special callers)."""
    explicit = httpx.Timeout(connect=1, read=2, write=3, pool=4)
    t = _build_httpx_timeout(explicit)
    assert t.connect == 1
    assert t.read == 2
    assert t.write == 3
    assert t.pool == 4


# ---------------------------------------------------------------------------
# Integration tests: real backend server behind proxy_http_request
# ---------------------------------------------------------------------------


class _SlowHandler(BaseHTTPRequestHandler):
    """Backend that delays before responding, simulating a long-poll."""

    delay: float = 0.0  # overridden per-server via a subclass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        time.sleep(type(self).delay)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence test noise
        return None


def _start_backend(delay: float) -> tuple[ThreadingHTTPServer, int]:
    handler = type("_H", (_SlowHandler,), {"delay": delay})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _free_port() -> int:
    """Return a port number that nothing is listening on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _make_request(port: int, path: str = "/sync") -> Request[Any, Any, Any]:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, Any]) -> None:
        return None

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", port),
        "scheme": "http",
    }
    return Request(scope, receive, send)  # type: ignore[arg-type]


async def _drain(response: ASGIResponse | ASGIStreamingResponse) -> tuple[int | None, bytes]:
    """Run an ASGIResponse through a fake ASGI cycle; capture status + body."""
    status: dict[str, int | None] = {"code": None}
    body_parts: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    await response(scope, receive, send)  # type: ignore[arg-type]
    return status["code"], b"".join(body_parts)


@pytest.mark.asyncio
async def test_long_poll_beyond_connect_timeout_is_not_cut_off() -> None:
    """A backend that takes far longer than the connect timeout still succeeds.

    Core regression: previously a 30s scalar timeout returned 504 for a request
    the backend was still legitimately serving.  We simulate a long-poll that
    outlasts a deliberately tiny connect timeout and assert we get the real 200,
    not a 504.
    """
    server, port = _start_backend(delay=1.5)
    try:
        request = _make_request(port)
        # Connect timeout 0.3s: the backend connects instantly but doesn't send
        # a response for 1.5s.  With a read timeout this would 504; it must not.
        response = await proxy_http_request(request, target_port=port, timeout=0.3)
        code, body = await _drain(response)
        assert code == 200, f"expected 200, got {code} (long-poll was cut off)"
        assert b'"ok": true' in body
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_dead_app_fails_fast_with_error_status() -> None:
    """A port with nothing listening fails quickly, not hang.

    The read timeout is open, so the fast-fail must come from the connect
    timeout: it must return promptly (well under the old 30s) with a gateway
    error status (502/504), never a 200.
    """
    port = _free_port()
    request = _make_request(port)
    t0 = time.monotonic()
    response = await proxy_http_request(request, target_port=port, timeout=1.0)
    code, _ = await _drain(response)
    elapsed = time.monotonic() - t0
    assert code in (502, 504), f"dead app should be a gateway error, got {code}"
    assert elapsed < 10, f"dead-app request took too long ({elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_normal_fast_request_succeeds() -> None:
    """A normal, fast backend response is proxied unchanged."""
    server, port = _start_backend(delay=0.0)
    try:
        request = _make_request(port)
        response = await proxy_http_request(request, target_port=port)
        code, body = await _drain(response)
        assert code == 200
        assert b'"ok": true' in body
    finally:
        server.shutdown()


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_PROXY_TEST") != "1",
    reason="slow (>30s); set RUN_SLOW_PROXY_TEST=1 to run",
)
@pytest.mark.asyncio
async def test_default_timeout_allows_request_past_30s() -> None:
    """With the default timeout, a >30s backend response is NOT cut to 504.

    Directly targets the reported bug threshold: the old default cut requests at
    30s.  Uses a backend that responds at ~31s (just past the old cutoff) and
    asserts success.  Slow; opt in with ``RUN_SLOW_PROXY_TEST=1``.
    """
    server, port = _start_backend(delay=31.0)
    try:
        request = _make_request(port)
        # No explicit timeout -> uses the default (connect fast, read open).
        response = await proxy_http_request(request, target_port=port)
        code, body = await _drain(response)
        assert code == 200, f"default timeout cut off a 31s request (got {code})"
        assert b'"ok": true' in body
    finally:
        server.shutdown()
