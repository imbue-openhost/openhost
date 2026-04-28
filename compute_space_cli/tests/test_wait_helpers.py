"""Tests for :func:`compute_space_cli.helpers.wait_for_app_removed`.

The function does HTTP polling against ``/api/app_status/<name>`` and
exits the CLI on terminal states. We mock ``make_api_request`` and
``time.sleep`` so the tests run instantly without a real server.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import httpx
import pytest

from compute_space_cli.helpers import wait_for_app_removed


def _resp(
    status_code: int,
    json_body: dict[str, object] | None = None,
    raise_on_json: bool = False,
) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    if raise_on_json:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = json_body or {}
    return r


def test_returns_on_404() -> None:
    """The happy completion path: a 404 means the row is gone."""
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            return_value=_resp(404),
        ),
    ):
        # Must return without raising.
        wait_for_app_removed("https://x", "tok", "myapp")


def test_returns_after_polling_through_removing() -> None:
    """Status='removing' on a 200 should keep polling until 404."""
    responses = [
        _resp(200, {"status": "removing"}),
        _resp(200, {"status": "removing"}),
        _resp(404),
    ]
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            side_effect=responses,
        ),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_exits_on_error_status() -> None:
    """If the worker fails it sets status='error'; the CLI must exit
    nonzero so callers (scripts, CI) see the failure rather than
    silently waiting forever."""
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            return_value=_resp(200, {"status": "error", "error": "boom"}),
        ),
    ):
        with pytest.raises(SystemExit) as exc:
            wait_for_app_removed("https://x", "tok", "myapp")
        assert exc.value.code == 1


def test_exits_on_unexpected_http_status() -> None:
    """A non-404 non-2xx response (e.g. 500) should fail fast rather
    than busy-looping."""
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            return_value=_resp(500),
        ),
    ):
        with pytest.raises(SystemExit) as exc:
            wait_for_app_removed("https://x", "tok", "myapp")
        assert exc.value.code == 1


def test_skips_unparseable_body_and_keeps_polling() -> None:
    """A 2xx with a non-JSON body (e.g. an upstream proxy HTML page
    during a restart) is transient — keep polling rather than crashing
    the CLI with an unhandled JSONDecodeError."""
    responses = [
        _resp(200, raise_on_json=True),  # garbage body
        _resp(200, {"status": "removing"}),
        _resp(404),
    ]
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            side_effect=responses,
        ),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_keeps_polling_through_network_errors() -> None:
    """A transient network error (connection refused during restart,
    timeout, etc.) must NOT crash the CLI — keep polling so we recover
    when the server comes back."""
    # ``side_effect`` with a list raises items that are exception
    # instances and returns items that aren't, in order.
    side_effect = [httpx.ConnectError("connection refused"), _resp(404)]

    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch(
            "compute_space_cli.helpers.make_api_request",
            side_effect=side_effect,
        ),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_exits_on_overall_timeout() -> None:
    """If the worker is stuck and the row never disappears, the CLI
    must give up and tell the operator rather than poll forever.

    We simulate a stuck worker by keeping the response at 'removing'
    and making ``time.time()`` jump past the deadline on the second
    iteration so the loop exits with the timeout message.
    """
    times = iter([0.0, 0.0, 9999.0])  # start, first-loop check, expired

    def _time() -> float:
        try:
            return next(times)
        except StopIteration:
            return 9999.0

    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.time.time", side_effect=_time),
        patch(
            "compute_space_cli.helpers.make_api_request",
            return_value=_resp(200, {"status": "removing"}),
        ),
    ):
        with pytest.raises(SystemExit) as exc:
            wait_for_app_removed("https://x", "tok", "myapp", timeout=5)
        assert exc.value.code == 1
