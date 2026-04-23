"""Tests for the host-prep fields on /api/settings/check_for_updates
and the HTTP 409 gate on /api/settings/update_repo_state.

These protect the Docker → podman transition and any future runtime
upgrade: /api/settings/update_repo_state must refuse to apply updates
when the host isn't ready, and /api/settings/check_for_updates must
surface the exact reason/message the dashboard banner renders.

The handlers are async Quart views; to exercise them in isolation we
call the underlying coroutine via `.__wrapped__` (bypassing the
login_required decorator) inside a test_request_context so the Quart
globals they depend on are populated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.runtime_sentinel import HostPrepStatus
from compute_space.core.updates import GitState


def _make_app_with_repo(repo_path: Path) -> Quart:
    """Build a Quart app with the minimal ``openhost_config`` attribute
    the settings handlers read.  Shared by every test in this module
    because check_for_updates/update_repo_state both look up
    config.openhost_repo_path and nothing else from the config object.
    """

    class _Cfg:
        openhost_repo_path = repo_path

    app = Quart(__name__)
    app.openhost_config = _Cfg()  # type: ignore[attr-defined]
    return app


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_ok_and_sentinel_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: both signals OK means ``host_prep_ok=True`` and no
    reason/message fields appear in the response."""

    async def fake_check_git_state(_repo_path):  # type: ignore[no-untyped-def]

        return GitState.UP_TO_DATE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["ok"] is True
    assert payload["host_prep_ok"] is True
    assert payload["container_runtime_available"] is True
    # When everything is OK the response must NOT include a
    # remediation message; that's the UI's signal to hide the banner.
    assert "host_prep_reason" not in payload
    assert "host_prep_message" not in payload


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_missing_as_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When podman is missing, the response must say so with the
    shared CONTAINER_RUNTIME_MISSING_ERROR text — even if the sentinel happened
    to claim the host is prepared.  The live probe is the authoritative
    signal for the Docker → podman transition because pre-upgrade hosts
    never had sentinels."""

    async def fake_check_git_state(_repo_path):  # type: ignore[no-untyped-def]

        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    # Sentinel says "all good" but podman is actually missing — the
    # response must STILL surface the podman-missing reason.
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["ok"] is True
    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_message"] == CONTAINER_RUNTIME_MISSING_ERROR


@pytest.mark.asyncio
async def test_check_for_updates_surfaces_sentinel_mismatch_when_podman_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When podman IS installed but the sentinel reports the wrong
    runtime_version (future-upgrade scenario), the response must carry
    the sentinel's specific reason/message so the banner explains what
    to do."""

    async def fake_check_git_state(_repo_path):  # type: ignore[no-untyped-def]

        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "re-run ansible to bump runtime_version"),
    )

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is True
    assert payload["host_prep_reason"] == "wrong_version"
    assert "ansible" in payload["host_prep_message"]


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_podman_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server-side safety gate: even if the dashboard banner is
    bypassed (direct curl, older cached page, CLI), update_repo_state
    must refuse with 409 and NOT touch the git working tree when the
    host isn't prepared.

    This is the belt-and-braces protection for future upgrades where
    the currently-running code knows how to gate; for the initial
    Docker → podman transition the pre-upgrade router handles this
    endpoint and the protection comes from _check_app_status instead."""

    # If this was called we'd know the gate failed.
    async def boom(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        result = await settings_mod.update_repo_state.__wrapped__()
    # Quart returns a (Response, status_code) tuple when the view
    # explicitly includes a status code.
    assert isinstance(result, tuple)
    response, status_code = result
    assert status_code == 409
    payload = await response.get_json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_ok"] is False


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_sentinel_mismatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sentinel half of the gate: podman is present but the host
    hasn't been re-prepped for this runtime_version."""

    async def boom(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "bump required"),
    )

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        result = await settings_mod.update_repo_state.__wrapped__()
    assert isinstance(result, tuple)
    response, status_code = result
    assert status_code == 409
    payload = await response.get_json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "wrong_version"


@pytest.mark.asyncio
async def test_update_repo_state_proceeds_when_host_prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When both signals say the host is ready, update_repo_state must
    actually invoke the git checkout rather than spuriously blocking."""

    called = {"n": 0}

    async def fake_get_current_ref(_repo_path):  # type: ignore[no-untyped-def]
        return "main"

    async def fake_checkout(_repo_path, _ref):  # type: ignore[no-untyped-def]
        called["n"] += 1

    monkeypatch.setattr(settings_mod, "get_current_ref", fake_get_current_ref)
    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", fake_checkout)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        response = await settings_mod.update_repo_state.__wrapped__()
        payload = await response.get_json()

    assert called["n"] == 1
    assert payload == {"ok": True}
