"""Tests for :func:`compute_space.testing.wait_app_removed`.

The CLI's sibling helper has its own suite under ``compute_space_cli/tests/``;
this file pins the test-helper's contract so integration tests that depend
on it don't degrade silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from _pytest.outcomes import Failed

from compute_space.testing import wait_app_removed


def _resp(status_code: int, json_body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    return r


def test_returns_on_404() -> None:
    """The completion path: a 404 means the row is gone."""
    session = MagicMock()
    session.get.return_value = _resp(404)
    # Must return without raising.
    wait_app_removed(session, "https://x", "myapp", timeout=5)


def test_polls_through_removing_then_returns_on_404() -> None:
    """A 200 with status='removing' should keep polling until the row
    actually disappears (404)."""
    session = MagicMock()
    session.get.side_effect = [
        _resp(200, {"status": "removing"}),
        _resp(200, {"status": "removing"}),
        _resp(404),
    ]
    wait_app_removed(session, "https://x", "myapp", timeout=30)


def test_pytest_fail_on_status_error() -> None:
    """If the worker fails it sets status='error'; the helper must
    fail the test so the integration suite catches removal regressions
    instead of polling forever."""
    session = MagicMock()
    session.get.return_value = _resp(200, {"status": "error", "error": "boom"})
    with pytest.raises(Failed):
        wait_app_removed(session, "https://x", "myapp", timeout=5)


def test_pytest_fail_on_timeout() -> None:
    """If the row stays in 'removing' past the timeout, the helper
    must call pytest.fail rather than hanging the suite."""
    session = MagicMock()
    session.get.return_value = _resp(200, {"status": "removing"})
    # Tiny timeout so the test runs fast. Patching sleep both speeds
    # the loop up and ensures the test doesn't depend on real wall-
    # clock interval pacing in poll().
    with patch("compute_space.testing.time.sleep"):
        with pytest.raises(Failed):
            wait_app_removed(session, "https://x", "myapp", timeout=0.05)
