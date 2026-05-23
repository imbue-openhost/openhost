"""Tests for the host-prep fields on /api/settings/check_for_updates
and the HTTP 409 gate on /api/settings/update_repo_state.

The handlers are Litestar route handlers — to exercise them in isolation
we call the underlying coroutine via ``handler.fn(...)``, passing
dependencies directly instead of going through Litestar's DI.
On the error path the handler raises ``HTTPException``; we inspect its
``status_code`` / ``detail`` / ``extra`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import cast

import pytest
from litestar.exceptions import HTTPException

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

    result = await settings_mod.check_for_updates.fn()

    assert isinstance(result, settings_mod.CheckUpdatesOk)
    assert result.host_prep_ok is True


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

    result = await settings_mod.check_for_updates.fn()

    assert isinstance(result, settings_mod.CheckUpdatesBlocked)
    assert result.host_prep_ok is False
    assert result.host_prep_reason == "behind"


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_not_prepared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom() -> None:
        raise AssertionError("agent_apply must not run when the gate fires")

    async def fake_agent_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="migrations needed", current_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "agent_apply", boom)
    monkeypatch.setattr(settings_mod, "agent_status", fake_agent_status)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.update_repo_state.fn()
    assert excinfo.value.status_code == 409
    assert excinfo.value.extra is not None
    extra = cast(dict[str, Any], excinfo.value.extra)
    assert extra["host_prep_reason"] == "behind"
    assert extra["host_prep_ok"] is False


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

    await settings_mod.update_repo_state.fn()
    assert called["n"] == 1
