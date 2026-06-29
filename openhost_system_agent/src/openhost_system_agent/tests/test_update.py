"""Tests for the tag-based update flow, run against real throwaway git repos.

Each test builds an actual git repo (and, where fetch matters, a local
file-based "origin" remote) in a tmp dir and points ``update._repo`` at it.
This exercises the real tag math, ``git describe``, checkout and ``git clean``
rather than mocking gitpython, while staying fast and offline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import git
import pytest

import openhost_system_agent.update as update_mod

# Fully isolate from the developer's global/system git config (signing, hooks,
# user identity) so tests are deterministic on any machine.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path: Path, msg: str = "wip") -> None:
    (path / "VERSION").write_text(msg + "\n")
    _git(path, "add", "VERSION")
    _git(path, "commit", "-m", msg)


def _commit_and_tag(path: Path, tag: str) -> None:
    _commit(path, f"release {tag}")
    _git(path, "tag", tag)


def _make_repo(path: Path, tags: list[str]) -> Path:
    """A standalone repo with one commit per tag (and no remote)."""
    path.mkdir(parents=True)
    _git(path, "-c", "init.defaultBranch=main", "init")
    for tag in tags:
        _commit_and_tag(path, tag)
    return path


def _clone_at(remote: Path, dest: Path, checkout: str | None = None) -> git.Repo:
    """Clone ``remote`` (so ``origin`` is set), optionally detaching at a tag."""
    _git(dest.parent, "clone", str(remote), dest.name)
    if checkout is not None:
        _git(dest, "checkout", checkout)
    return git.Repo(dest)


def _stub_execv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("openhost_system_agent.update.os.execv", lambda *a: calls.append(a))
    return calls


# ── Pure tag helpers ─────────────────────────────────────────────────


def test_version_key_orders_release_tags() -> None:
    vk = update_mod._version_key
    assert vk("v1") < vk("v1.0") < vk("v1.0.0") < vk("v2") < vk("v10")
    assert vk("v1.2.0") < vk("v1.10.0")  # numeric, not lexicographic


def test_get_sorted_tags_filters_non_release_and_sorts(tmp_path: Path) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v2.0.0", "v1.10.0", "v1.2.0"])
    _git(repo_dir, "tag", "v1.0.0-rc1")  # pre-release: ignored
    _git(repo_dir, "tag", "nightly")  # non-release: ignored
    repo = git.Repo(repo_dir)

    assert update_mod._get_sorted_tags(repo) == ["v1.2.0", "v1.10.0", "v2.0.0"]


def test_current_and_ancestor_tag(tmp_path: Path) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v1.0.0", "v1.1.0"])
    repo = git.Repo(repo_dir)

    assert update_mod._current_tag(repo) == "v1.1.0"
    assert update_mod._latest_ancestor_tag(repo) == "v1.1.0"

    # Move past the tag: no longer *exactly* on it, but it's still the ancestor.
    _commit(repo_dir, "past the tag")
    assert update_mod._current_tag(repo) is None
    assert update_mod._latest_ancestor_tag(repo) == "v1.1.0"


# ── fetch_updates ────────────────────────────────────────────────────


def test_fetch_updates_behind_after_remote_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    _commit_and_tag(remote, "v1.1.0")  # new release the local doesn't have yet
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    # fetch_updates fetches origin --tags, so it discovers v1.1.0.
    assert update_mod.fetch_updates().state == "BEHIND_REMOTE"


def test_fetch_updates_up_to_date_on_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_fetch_updates_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    (tmp_path / "local" / "untracked").write_text("x")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "DIRTY"


def test_fetch_updates_without_origin_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = _make_repo(tmp_path / "repo", ["v1.0.0"])  # no remote
    monkeypatch.setattr(update_mod, "_repo", lambda: git.Repo(repo_dir))

    with pytest.raises(RuntimeError, match="origin"):
        update_mod.fetch_updates()


# ── apply_update ─────────────────────────────────────────────────────


def test_apply_update_steps_onto_immediate_next_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v2.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    # Stepping-stone walk: we move to v1.1.0, NOT straight to the latest v2.0.0.
    assert update_mod._current_tag(local) == "v1.1.0"
    assert len(calls) == 1  # handed off to the apply walk exactly once


def test_apply_update_on_latest_hands_off_without_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.1.0")
    head_before = local.head.commit.hexsha
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    assert local.head.commit.hexsha == head_before  # no checkout happened
    assert len(calls) == 1


def test_apply_update_with_no_ancestor_tag_starts_at_first_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HEAD sits on a commit that predates every tag, so current is None.
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "-c", "init.defaultBranch=main", "init")
    _commit(remote, "base")  # untagged root commit
    _commit_and_tag(remote, "v1.0.0")  # tag is a descendant, not an ancestor
    local = _clone_at(remote, tmp_path / "local", checkout="HEAD~1")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod._latest_ancestor_tag(local) is None  # precondition
    update_mod.apply_update()

    assert update_mod._current_tag(local) == "v1.0.0"  # started the walk at tags[0]
    assert len(calls) == 1


def test_apply_update_dirty_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    (tmp_path / "local" / "untracked").write_text("x")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="uncommitted"):
        update_mod.apply_update()
    assert calls == []  # never handed off


def test_apply_update_without_tags_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "-c", "init.defaultBranch=main", "init")
    _commit(remote, "only commit, no tags")
    local = _clone_at(remote, tmp_path / "local")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    with pytest.raises(RuntimeError, match="No tags"):
        update_mod.apply_update()
    assert calls == []


# ── show_diff / get_remote_info ──────────────────────────────────────


def test_show_diff_lists_commits_between_current_and_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0", "v1.2.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    diff = update_mod.show_diff()

    assert diff.current_ref == "v1.0.0"
    assert diff.remote_ref == "v1.2.0"
    assert len(diff.commits) == 2  # the v1.1.0 and v1.2.0 commits


def test_get_remote_info_reports_current_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    info = update_mod.get_remote_info()

    assert info.ref == "v1.0.0"
    assert info.url is not None and "remote" in info.url


# ── Pinned target ref (run from a branch/commit) ─────────────────────


def _branch(remote: Path, name: str, msg: str) -> None:
    """Create a feature branch one commit ahead of the current HEAD."""
    _git(remote, "checkout", "-b", name)
    _commit(remote, msg)
    _git(remote, "checkout", "main")


def test_next_step_walks_tags_first_then_pinned_target(tmp_path: Path) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")
    repo = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(repo, "feature")

    # Behind in tags: still step through the tag stones first.
    assert update_mod._next_step(repo) == "v1.1.0"

    # On the latest tag: the pinned branch is the final hop.
    _git(tmp_path / "local", "checkout", "v1.1.0")
    assert update_mod._next_step(repo) == "feature"

    # On the branch tip: nothing left to do.
    tip = update_mod._resolve_ref_sha(repo, "feature")
    assert tip is not None
    _git(tmp_path / "local", "checkout", tip)
    assert update_mod._next_step(repo) is None


def test_apply_update_pinned_still_steps_through_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0", "v1.1.0"])
    _branch(remote, "feature", "feature work")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    calls = _stub_execv(monkeypatch)
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.apply_update()

    # First step is the next tag, not a jump straight to the pinned branch.
    assert update_mod._current_tag(local) == "v1.1.0"
    assert len(calls) == 1


def test_fetch_updates_pinned_behind_when_branch_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    _git(remote, "checkout", "feature")
    _commit(remote, "f2")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "BEHIND_REMOTE"


def test_fetch_updates_pinned_up_to_date_on_branch_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="feature")
    update_mod._set_target_ref(local, "feature")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.fetch_updates().state == "UP_TO_DATE"


def test_set_remote_url_pins_and_clears_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    update_mod.set_remote_url(f"file://{remote}@feature")
    assert update_mod._get_target_ref(local) == "feature"

    update_mod.set_remote_url(f"file://{remote}")
    assert update_mod._get_target_ref(local) is None


def test_get_remote_info_reports_pinned_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = _make_repo(tmp_path / "remote", ["v1.0.0"])
    _branch(remote, "feature", "f1")
    local = _clone_at(remote, tmp_path / "local", checkout="v1.0.0")
    update_mod._set_target_ref(local, "feature")
    monkeypatch.setattr(update_mod, "_repo", lambda: local)

    assert update_mod.get_remote_info().ref == "feature"
