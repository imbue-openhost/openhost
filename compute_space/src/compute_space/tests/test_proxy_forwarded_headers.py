"""Unit tests for the request-header forwarding helpers in web/helpers/proxy.py."""

from litestar.datastructures import Headers

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.web.helpers.proxy import _HTTP_REQUEST_EXCLUDED_HEADERS
from compute_space.web.helpers.proxy import _build_forwarded_request_headers
from compute_space.web.helpers.proxy import _canonicalize_header_name


def _build(raw: list[tuple[bytes, bytes]], extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    return _build_forwarded_request_headers(Headers(raw), _HTTP_REQUEST_EXCLUDED_HEADERS, extra or [])


def test_canonicalize_header_name() -> None:
    assert _canonicalize_header_name("authorization") == "Authorization"
    assert _canonicalize_header_name("content-type") == "Content-Type"
    assert _canonicalize_header_name("x-real-ip") == "X-Real-Ip"


def test_forwarded_headers_are_canonically_cased() -> None:
    """ASGI lowercases inbound header names; backends that read names case-sensitively (e.g. PHP's
    getallheaders()) must still see the conventional Title-Case form."""
    result = _build([(b"authorization", b"Bearer tok"), (b"x-custom-thing", b"v")])
    assert ("Authorization", "Bearer tok") in result
    assert ("X-Custom-Thing", "v") in result


def test_excluded_and_openhost_headers_still_dropped() -> None:
    result = _build(
        [
            (b"host", b"example.com"),
            (b"connection", b"keep-alive"),
            (b"x-openhost-is-owner", b"true"),
            (b"accept", b"*/*"),
        ]
    )
    assert result == [("Accept", "*/*")]


def test_session_cookie_still_stripped() -> None:
    result = _build([(b"cookie", f"{SESSION_COOKIE_NAME}=secret; other=1".encode())])
    assert result == [("Cookie", "other=1")]


def test_extra_headers_keep_given_casing() -> None:
    result = _build([(b"accept", b"*/*")], extra=[("X-OpenHost-Is-Owner", "true")])
    assert ("X-OpenHost-Is-Owner", "true") in result
