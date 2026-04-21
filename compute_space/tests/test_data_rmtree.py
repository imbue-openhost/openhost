"""Tests for the shared rmtree_with_sudo_fallback helper in core/data.py.

The helper implements a two-stage cleanup:

1. ``shutil.rmtree`` with an ``onexc`` hook that chmods read-only entries
   and retries.
2. ``sudo -n rm -rf`` for files whose owner the router can't chmod.

Both stages must log-and-swallow when ``raise_on_failure=False`` and
re-raise when ``raise_on_failure=True``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import compute_space.core.data as data_mod
from compute_space.core.data import rmtree_with_sudo_fallback


def _make_tree(root: Path) -> None:
    """Create a small tree with readable and read-only files."""
    root.mkdir()
    (root / "a.txt").write_text("a")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("b")


def test_rmtree_fast_path_deletes_entire_tree(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    _make_tree(root)

    rmtree_with_sudo_fallback(str(root))

    assert not root.exists()


def test_rmtree_onexc_hook_chmods_readonly_files(tmp_path: Path) -> None:
    """Git clones frequently leave read-only files that a naive rmtree
    cannot delete.  The onexc hook should chmod and retry."""
    root = tmp_path / "notes"
    _make_tree(root)
    # Make the sub-directory's file and the dir itself read-only.
    os.chmod(root / "sub" / "b.txt", stat.S_IRUSR)
    os.chmod(root / "sub", stat.S_IRUSR | stat.S_IXUSR)

    rmtree_with_sudo_fallback(str(root))

    assert not root.exists()


def test_rmtree_missing_path_is_noop(tmp_path: Path) -> None:
    """Deprovision code paths can be called for apps that never fully
    provisioned; an already-gone path is not an error."""
    target = tmp_path / "never-existed"
    # Does not raise.
    rmtree_with_sudo_fallback(str(target))


def test_rmtree_swallows_sudo_failure_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With raise_on_failure=False, a failed sudo rm is logged but does
    not propagate — cleanup failures mustn't block DB row removal."""
    root = tmp_path / "sticky"
    _make_tree(root)

    # Force shutil.rmtree to always fail with a non-permission OSError
    # so the sudo fallback path runs.
    def fake_rmtree(path, onexc=None):  # type: ignore[no-untyped-def]
        raise OSError("simulated EBUSY")

    # And force the sudo fallback to fail too.
    def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd, stderr=b"sudo: a password is required\n")

    monkeypatch.setattr(data_mod.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(data_mod.subprocess, "run", fake_run)

    # Must not raise.
    rmtree_with_sudo_fallback(str(root), raise_on_failure=False)


def test_rmtree_reraises_sudo_failure_when_opted_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code-sync path calls with raise_on_failure=True so operators
    notice when their sudoers rule breaks."""
    root = tmp_path / "sticky"
    _make_tree(root)

    def fake_rmtree(path, onexc=None):  # type: ignore[no-untyped-def]
        raise OSError("simulated EBUSY")

    def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd, stderr=b"sudo: a password is required\n")

    monkeypatch.setattr(data_mod.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(data_mod.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        rmtree_with_sudo_fallback(str(root), raise_on_failure=True)


def test_rmtree_sudo_success_after_rmtree_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When shutil.rmtree fails but sudo succeeds, the directory ends
    up gone and no exception is raised."""
    root = tmp_path / "sticky"
    _make_tree(root)

    def fake_rmtree(path, onexc=None):  # type: ignore[no-untyped-def]
        raise PermissionError("permission denied")

    captured: list[list[str]] = []

    def fake_run(cmd, **_kw):  # type: ignore[no-untyped-def]
        captured.append(list(cmd))
        # Simulate sudo actually removing the tree.  Use os.walk + os.remove /
        # os.rmdir because shutil.rmtree has been monkey-patched out above.
        target = cmd[-1]
        for dirpath, dirnames, filenames in os.walk(target, topdown=False):
            for name in filenames:
                os.remove(os.path.join(dirpath, name))
            for name in dirnames:
                os.rmdir(os.path.join(dirpath, name))
        os.rmdir(target)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(data_mod.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(data_mod.subprocess, "run", fake_run)

    rmtree_with_sudo_fallback(str(root))

    assert not root.exists()
    assert captured[0][:3] == ["sudo", "-n", "rm"]
