"""Smoke test: before owner setup, any request must redirect to /setup.

This catches the failure mode where a route handler calls ``url_for`` for
an endpoint that doesn't exist (e.g. ``auth.setup`` after the setup view
moved to its own blueprint) — Werkzeug raises ``BuildError`` and the
server returns 500 for *every* request before setup, locking the operator
out of their own instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.web.app import create_app

from .conftest import _make_test_config


@pytest.mark.asyncio
async def test_root_redirects_to_setup_on_fresh_db(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, port=20500)
    app = create_app(cfg)

    client = app.test_client()
    resp = await client.get("/")

    assert resp.status_code == 302, f"expected redirect to /setup, got {resp.status_code}"
    location = resp.headers.get("Location", "")
    assert location.endswith("/setup"), f"expected redirect to /setup, got {location!r}"


@pytest.mark.asyncio
async def test_login_page_redirects_to_setup_on_fresh_db(tmp_path: Path) -> None:
    """The /login view itself also redirects to /setup when no owner exists.

    Regression for the same class of bug — /login built ``url_for('auth.setup')``
    which 500s once setup moved to its own blueprint.
    """
    cfg = _make_test_config(tmp_path, port=20501)
    app = create_app(cfg)

    client = app.test_client()
    resp = await client.get("/login")

    assert resp.status_code == 302, f"expected redirect, got {resp.status_code}"
    assert resp.headers.get("Location", "").endswith("/setup")
