from __future__ import annotations

from pathlib import Path

import git
import pytest

import openhost_system_agent.update as update_mod


class _Commit:
    hexsha = "a" * 40


class _Head:
    commit = _Commit()
    is_detached = False


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


# --- detached HEAD recovery (real git repos) --------------------------------


def _make_clone_with_remote(tmp_path: Path) -> git.Repo:
    """Create a bare 'remote' with two commits on main and a working clone of it."""
    remote_path = tmp_path / "remote.git"
    git.Repo.init(remote_path, bare=True, initial_branch="main")

    work_path = tmp_path / "work"
    repo = git.Repo.clone_from(remote_path, work_path)
    repo.config_writer().set_value("user", "name", "t").release()
    repo.config_writer().set_value("user", "email", "t@example.com").release()

    (work_path / "f.txt").write_text("v1\n")
    repo.index.add(["f.txt"])
    repo.index.commit("c1")
    repo.git.branch("-M", "main")
    repo.git.push("origin", "main")

    (work_path / "f.txt").write_text("v2\n")
    repo.index.add(["f.txt"])
    repo.index.commit("c2")
    repo.git.push("origin", "main")

    # Point origin/HEAD at main so the recovery default-branch lookup resolves.
    repo.git.remote("set-head", "origin", "main")
    return repo


def test_fetch_reports_detached_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_clone_with_remote(tmp_path)
    first_sha = repo.git.rev_list("--max-parents=0", "HEAD").strip()
    repo.git.checkout(first_sha)
    assert repo.head.is_detached

    monkeypatch.setattr(update_mod, "_repo", lambda: repo)

    result = update_mod.fetch_updates()

    assert result.state == "DETACHED_HEAD"


def test_apply_recovers_from_detached_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_clone_with_remote(tmp_path)
    latest_sha = repo.head.commit.hexsha
    first_sha = repo.git.rev_list("--max-parents=0", "HEAD").strip()
    repo.git.checkout(first_sha)
    assert repo.head.is_detached

    pixi_calls = {"n": 0}
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)
    monkeypatch.setattr(update_mod, "_run_pixi_install", lambda: pixi_calls.__setitem__("n", pixi_calls["n"] + 1))
    monkeypatch.setattr(update_mod, "_run_migrations_reexec", lambda: [2])

    result = update_mod.apply_update()

    # HEAD is back on a real branch, tracking origin/main, at the latest commit.
    assert not repo.head.is_detached
    assert repo.active_branch.name == "main"
    assert repo.active_branch.tracking_branch().name == "origin/main"
    assert repo.head.commit.hexsha == latest_sha
    assert result.ref == latest_sha[:8]
    assert result.system_migrations_applied == [2]
    assert result.already_up_to_date is False
    # Recovering from an OLD detached commit changes the code, so deps must be
    # reinstalled (regression guard: the pre-recovery sha must drive this).
    assert pixi_calls["n"] == 1


def test_apply_recovers_from_detached_head_at_tip_skips_pixi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_clone_with_remote(tmp_path)
    latest_sha = repo.head.commit.hexsha
    # Detach at the tip of main — same commit as origin/main.
    repo.git.checkout(latest_sha)
    assert repo.head.is_detached

    pixi_calls = {"n": 0}
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)
    monkeypatch.setattr(update_mod, "_run_pixi_install", lambda: pixi_calls.__setitem__("n", pixi_calls["n"] + 1))
    monkeypatch.setattr(update_mod, "_run_migrations_reexec", list)

    result = update_mod.apply_update()

    assert not repo.head.is_detached
    assert repo.active_branch.name == "main"
    assert repo.head.commit.hexsha == latest_sha
    # No code change → no pixi install, just migrations.
    assert pixi_calls["n"] == 0
    assert result.already_up_to_date is True


def test_apply_detached_head_errors_clearly_without_remote_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_clone_with_remote(tmp_path)
    first_sha = repo.git.rev_list("--max-parents=0", "HEAD").strip()
    repo.git.checkout(first_sha)
    # Drop the remote-tracking refs so there is nothing to recover onto.
    repo.git.update_ref("-d", "refs/remotes/origin/HEAD")
    repo.git.update_ref("-d", "refs/remotes/origin/main")

    monkeypatch.setattr(update_mod, "_repo", lambda: repo)

    with pytest.raises(RuntimeError, match="HEAD is detached"):
        update_mod.apply_update()
