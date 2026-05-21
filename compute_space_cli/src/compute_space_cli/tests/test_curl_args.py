"""Unit tests for the ``oh curl`` arg-rewriting helper."""

from compute_space_cli.main import _rewrite_curl_args

BASE = "https://host.example.com"


def test_path_only_args_get_base_prefixed() -> None:
    assert _rewrite_curl_args(["/api/apps"], BASE) == ["https://host.example.com/api/apps"]


def test_full_urls_pass_through() -> None:
    assert _rewrite_curl_args(["https://other.example.com/x"], BASE) == ["https://other.example.com/x"]


def test_method_after_dash_x_is_not_url_prefixed() -> None:
    # Regression: the old heuristic ("any non-dashed arg is a URL")
    # mangled `-X POST` into `-X https://host.example.com/POST`,
    # which curl then sent as `:method: https://host/POST`.
    args = ["-X", "POST", "/api/foo"]
    assert _rewrite_curl_args(args, BASE) == [
        "-X",
        "POST",
        "https://host.example.com/api/foo",
    ]


def test_header_value_passes_through() -> None:
    args = ["-H", "Content-Type: application/json", "/api/foo"]
    assert _rewrite_curl_args(args, BASE) == [
        "-H",
        "Content-Type: application/json",
        "https://host.example.com/api/foo",
    ]


def test_data_value_passes_through() -> None:
    args = ["-d", '{"a":1}', "/api/foo"]
    assert _rewrite_curl_args(args, BASE) == [
        "-d",
        '{"a":1}',
        "https://host.example.com/api/foo",
    ]


def test_protocol_relative_url_is_left_alone() -> None:
    # ``//host/path`` is a protocol-relative URL — passing through
    # avoids double-prefixing to ``https://host.example.com//host/path``.
    args = ["//other.example.com/x"]
    assert _rewrite_curl_args(args, BASE) == ["//other.example.com/x"]


def test_base_url_trailing_slash_stripped() -> None:
    assert _rewrite_curl_args(["/api/x"], "https://host.example.com/") == ["https://host.example.com/api/x"]
