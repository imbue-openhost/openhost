"""Tests for :func:`compute_space_cli.helpers.wait_for_app_removed`."""

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
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.make_api_request", return_value=_resp(404)),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_returns_after_polling_through_removing() -> None:
    responses = [
        _resp(200, {"status": "removing"}),
        _resp(200, {"status": "removing"}),
        _resp(404),
    ]
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.make_api_request", side_effect=responses),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_exits_on_error_status() -> None:
    """If the worker reports status='error', exit nonzero so callers
    (scripts, CI) don't silently wait forever."""
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
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.make_api_request", return_value=_resp(500)),
    ):
        with pytest.raises(SystemExit) as exc:
            wait_for_app_removed("https://x", "tok", "myapp")
        assert exc.value.code == 1


def test_skips_unparseable_body_and_keeps_polling() -> None:
    """2xx with non-JSON body is transient (proxy HTML during restart)."""
    responses = [
        _resp(200, raise_on_json=True),
        _resp(200, {"status": "removing"}),
        _resp(404),
    ]
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.make_api_request", side_effect=responses),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_keeps_polling_through_network_errors() -> None:
    """Network errors (connection refused, timeout) are transient."""
    # side_effect raises items that are exception instances and returns others.
    side_effect = [httpx.ConnectError("connection refused"), _resp(404)]
    with (
        patch("compute_space_cli.helpers.time.sleep"),
        patch("compute_space_cli.helpers.make_api_request", side_effect=side_effect),
    ):
        wait_for_app_removed("https://x", "tok", "myapp")


def test_exits_on_overall_timeout() -> None:
    """A stuck worker must not hang the CLI forever."""
    times = iter([0.0, 0.0, 9999.0])

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
