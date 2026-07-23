"""Unit tests for the working-tree snapshot used to deploy the app under test."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from openhost_test_harness.stack import _snapshot_working_tree


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture(autouse=True)
def _allow_file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def test_snapshot_includes_tracked_untracked_and_skips_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "tracked.txt").write_text("tracked")
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    (repo / "untracked.txt").write_text("untracked")
    (repo / "ignored.txt").write_text("ignored")

    dest = tmp_path / "dest"
    _snapshot_working_tree(repo, dest)

    assert (dest / "tracked.txt").read_text() == "tracked"
    assert (dest / "untracked.txt").read_text() == "untracked"
    assert not (dest / "ignored.txt").exists()
    assert not (dest / ".git").exists()


def test_snapshot_includes_submodule_working_tree(tmp_path: Path) -> None:
    """git ls-files lists a submodule as a bare gitlink, so its files need their
    own pass — without it a submodule-based app deploys with the directory empty."""
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    _git(sub_origin, "init", "-b", "main")
    (sub_origin / "sub.txt").write_text("sub content")
    _git(sub_origin, "add", ".")
    _git(sub_origin, "commit", "-m", "sub")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "app.txt").write_text("app")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "submodule", "add", f"file://{sub_origin}", "vendor/sub")
    _git(repo, "commit", "-m", "add submodule")
    # Uncommitted submodule edits ride along too (same rationale as the superproject).
    (repo / "vendor/sub/local-edit.txt").write_text("edit")

    dest = tmp_path / "dest"
    _snapshot_working_tree(repo, dest)

    assert (dest / "app.txt").read_text() == "app"
    assert (dest / "vendor/sub/sub.txt").read_text() == "sub content"
    assert (dest / "vendor/sub/local-edit.txt").read_text() == "edit"
    assert not (dest / "vendor/sub/.git").exists()
