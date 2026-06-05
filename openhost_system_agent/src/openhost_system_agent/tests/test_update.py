from __future__ import annotations

import pytest

import openhost_system_agent.update as update_mod


class _Commit:
    hexsha = "a" * 40


class _Head:
    commit = _Commit()


class _Branch:
    name = "main"


class _Repo:
    active_branch = _Branch()
    head = _Head()

    def __init__(self) -> None:
        self.refs = {"origin/main": _Head()}

    def is_dirty(self, *, untracked_files: bool) -> bool:
        return False


def test_apply_update_runs_migrations_when_code_is_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mod, "_repo", _Repo)
    monkeypatch.setattr(update_mod, "_run_migrations_reexec", lambda: [2])

    result = update_mod.apply_update()

    assert result.ref == "aaaaaaaa"
    assert result.system_migrations_applied == [2]
    assert result.already_up_to_date is False


def test_apply_update_reports_up_to_date_when_no_migrations_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mod, "_repo", _Repo)
    monkeypatch.setattr(update_mod, "_run_migrations_reexec", list)

    result = update_mod.apply_update()

    assert result.ref == "aaaaaaaa"
    assert result.system_migrations_applied == []
    assert result.already_up_to_date is True
