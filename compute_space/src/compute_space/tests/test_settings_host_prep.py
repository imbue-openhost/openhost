"""Tests for the host-prep fields on /api/settings/check_for_updates
and the HTTP 409 gate on /api/settings/update_repo_state.

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
from compute_space.core.system_agent import ApplyResult
from compute_space.core.system_agent import FetchResult
from compute_space.core.system_agent import MigrationStatus


@pytest.mark.asyncio
async def test_check_for_updates_reports_host_prep_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_agent_fetch() -> FetchResult:
        return FetchResult(state="UP_TO_DATE")

    async def fake_agent_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "agent_fetch", fake_agent_fetch)
    monkeypatch.setattr(settings_mod, "agent_status", fake_agent_status)

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["ok"] is True
    assert payload["host_prep_ok"] is True
    assert "host_prep_reason" not in payload
    assert "host_prep_message" not in payload


@pytest.mark.asyncio
async def test_check_for_updates_surfaces_version_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_agent_fetch() -> FetchResult:
        return FetchResult(state="BEHIND_REMOTE")

    async def fake_agent_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="run system migrations", current_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "agent_fetch", fake_agent_fetch)
    monkeypatch.setattr(settings_mod, "agent_status", fake_agent_status)

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["host_prep_ok"] is False
    assert payload["host_prep_reason"] == "behind"


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_not_prepared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom() -> ApplyResult:
        raise AssertionError("agent_apply must not run when the gate fires")

    async def fake_agent_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="migrations needed", current_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "agent_apply", boom)
    monkeypatch.setattr(settings_mod, "agent_status", fake_agent_status)

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        result = await settings_mod.update_repo_state.__wrapped__()
    assert isinstance(result, tuple)
    response, status_code = result
    assert status_code == 409
    payload = await response.get_json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "behind"


@pytest.mark.asyncio
async def test_update_repo_state_proceeds_when_host_prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = {"n": 0}

    async def fake_apply() -> ApplyResult:
        called["n"] += 1
        return ApplyResult(ref="abc1234", system_migrations_applied=[])

    async def fake_agent_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "agent_status", fake_agent_status)

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        response = await settings_mod.update_repo_state.__wrapped__()
        payload = await response.get_json()

    assert called["n"] == 1
    assert payload == {"ok": True}
