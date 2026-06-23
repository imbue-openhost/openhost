from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from git import GitCommandError

import openhost_system_agent.update as update_mod
from openhost_system_agent.protocol import ApplyResult


def _make_repo(*, tags: list[str], current_tag: str | None = None, dirty: bool = False) -> MagicMock:
    repo = MagicMock()
    repo.is_dirty.return_value = dirty

    tag_objs = []
    for name in tags:
        t = MagicMock()
        t.name = name
        tag_objs.append(t)
    repo.tags = tag_objs

    if current_tag:
        repo.git.describe.return_value = current_tag
    else:
        repo.git.describe.side_effect = GitCommandError("describe", "no tag")

    repo.git.fetch.return_value = None
    repo.remote.return_value = MagicMock()
    return repo


def test_apply_update_already_on_latest_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tags=["v1.0.0"], current_tag="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)
    monkeypatch.setattr(
        update_mod,
        "_reexec_apply",
        lambda: ApplyResult(ref="v1.0.0", system_migrations_applied=[], already_up_to_date=True),
    )

    result = update_mod.apply_update()
    assert result.ref == "v1.0.0"
    assert result.already_up_to_date is True


def test_apply_update_checks_out_next_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tags=["v1.0.0", "v1.1.0"], current_tag="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)
    monkeypatch.setattr(
        update_mod,
        "_reexec_apply",
        lambda: ApplyResult(ref="v1.1.0", system_migrations_applied=[3], already_up_to_date=False),
    )

    result = update_mod.apply_update()
    repo.git.checkout.assert_called_with("v1.1.0")
    assert result.ref == "v1.1.0"
    assert result.system_migrations_applied == [3]


def test_fetch_updates_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tags=["v1.0.0", "v1.1.0"], current_tag="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)

    result = update_mod.fetch_updates()
    assert result.state == "BEHIND_REMOTE"


def test_fetch_updates_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tags=["v1.0.0"], current_tag="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)

    result = update_mod.fetch_updates()
    assert result.state == "UP_TO_DATE"


def test_fetch_updates_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tags=["v1.0.0"], current_tag="v1.0.0", dirty=True)
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)

    result = update_mod.fetch_updates()
    assert result.state == "DIRTY"
