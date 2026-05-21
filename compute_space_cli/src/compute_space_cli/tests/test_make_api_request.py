"""Tests for :func:`compute_space_cli.helpers.make_api_request`."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from compute_space_cli.helpers import make_api_request


def _ok_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {}
    return r


def test_post_with_data_sends_json_body() -> None:
    # Regression: the function previously passed ``data=`` to httpx,
    # which form-encodes — server endpoints typed against JSON models
    # then rejected the body with HTTP 400.
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok", "POST", "/api/foo", data={"k": "v"})
        kwargs = mock_req.call_args.kwargs
        assert kwargs.get("json") == {"k": "v"}
        assert "data" not in kwargs


def test_get_without_data_sends_no_body() -> None:
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok", "GET", "/api/foo")
        kwargs = mock_req.call_args.kwargs
        # httpx treats json=None as "no body" — same outcome as omitting.
        assert kwargs.get("json") is None
        assert "data" not in kwargs


def test_bearer_header_attached() -> None:
    with patch(
        "compute_space_cli.helpers.httpx.request",
        return_value=_ok_response(),
    ) as mock_req:
        make_api_request("https://x", "tok-123", "GET", "/api/foo")
        kwargs = mock_req.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
