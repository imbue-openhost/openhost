"""Tests for compute_space.core.runtime_sentinel.

The sentinel exists to stop existing Docker-era instances from
breaking themselves by clicking the dashboard Update button: the
router refuses to start unless /etc/openhost/runtime reports the
runtime + version its code expects, and the settings UI surfaces a
matching banner so the user can fix the host before clicking Update.

These tests exercise every branch of the parser and the two public
entry points (host_prep_status, check_runtime_sentinel) against a
sentinel written to tmp_path rather than the real /etc.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from compute_space.core import runtime_sentinel
from compute_space.core.runtime_sentinel import EXPECTED_RUNTIME
from compute_space.core.runtime_sentinel import EXPECTED_RUNTIME_VERSION
from compute_space.core.runtime_sentinel import RuntimeSentinelMismatch
from compute_space.core.runtime_sentinel import check_runtime_sentinel
from compute_space.core.runtime_sentinel import host_prep_status


def _write_sentinel(path: Path, runtime: str, version: str) -> None:
    path.write_text(f"runtime={runtime}\nruntime_version={version}\n")


def test_matching_sentinel_is_ok(tmp_path: Path) -> None:
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, str(EXPECTED_RUNTIME_VERSION))

    status = host_prep_status(str(sentinel))
    assert status.ok is True
    assert status.reason == ""


def test_matching_sentinel_passes_hard_check(tmp_path: Path) -> None:
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, str(EXPECTED_RUNTIME_VERSION))

    # Does not raise.
    check_runtime_sentinel(str(sentinel))


def test_missing_sentinel_is_not_ok(tmp_path: Path) -> None:
    """The most common failure mode: Docker-era host with no /etc/openhost."""
    sentinel = tmp_path / "does-not-exist"

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "missing"
    assert "ansible" in status.message.lower()


def test_missing_sentinel_raises_from_hard_check(tmp_path: Path) -> None:
    sentinel = tmp_path / "does-not-exist"
    with pytest.raises(RuntimeSentinelMismatch) as exc_info:
        check_runtime_sentinel(str(sentinel))
    # The message must be actionable to the operator reading journalctl.
    assert "ansible" in str(exc_info.value).lower()


def test_wrong_runtime_tag_is_rejected(tmp_path: Path) -> None:
    """Belt-and-braces: if a future migration writes runtime=containerd
    but the router still expects runtime=podman, we must fail closed."""
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, "containerd", str(EXPECTED_RUNTIME_VERSION))

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "wrong_runtime"


def test_wrong_version_is_rejected(tmp_path: Path) -> None:
    """Future runtime_version bumps must force a host re-prep, not be ignored."""
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, str(EXPECTED_RUNTIME_VERSION + 1))

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "wrong_version"
    assert str(EXPECTED_RUNTIME_VERSION) in status.message


def test_non_integer_version_is_rejected(tmp_path: Path) -> None:
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, "not-a-number")

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "wrong_version"


def test_sentinel_with_comments_and_blank_lines_is_parsed(tmp_path: Path) -> None:
    """ansible templates the file with a header comment; the parser must
    ignore comments and blank lines so the file stays human-editable."""
    sentinel = tmp_path / "runtime"
    sentinel.write_text(
        "# Managed by ansible/tasks/podman.yml\n"
        "\n"
        f"runtime={EXPECTED_RUNTIME}\n"
        "\n"
        f"runtime_version={EXPECTED_RUNTIME_VERSION}\n"
    )

    status = host_prep_status(str(sentinel))
    assert status.ok is True


def test_sentinel_ignores_unknown_keys(tmp_path: Path) -> None:
    """Forward compatibility: a future ansible version may add fields
    (e.g. kernel_version=6.8) that the current router doesn't know
    about.  Unknown keys must be silently ignored, not fail the gate."""
    sentinel = tmp_path / "runtime"
    sentinel.write_text(
        f"runtime={EXPECTED_RUNTIME}\n"
        f"runtime_version={EXPECTED_RUNTIME_VERSION}\n"
        "kernel_version=6.8.0-experimental\n"
        "something_else=whatever\n"
    )

    status = host_prep_status(str(sentinel))
    assert status.ok is True


def test_sentinel_ignores_non_key_value_lines(tmp_path: Path) -> None:
    sentinel = tmp_path / "runtime"
    sentinel.write_text(
        f"this line has no equals sign\nruntime={EXPECTED_RUNTIME}\nruntime_version={EXPECTED_RUNTIME_VERSION}\n"
    )

    status = host_prep_status(str(sentinel))
    assert status.ok is True


def test_unreadable_sentinel_surfaces_io_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If /etc/openhost/runtime is unreadable (permissions, corrupted
    filesystem, …), report the IO error cleanly rather than crashing
    the router with a bare OSError traceback."""
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, str(EXPECTED_RUNTIME_VERSION))

    # Monkey-patch open to simulate an unreadable file even though it exists.
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == str(sentinel):
            raise PermissionError("simulated permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "unreadable"
    assert "permission denied" in status.message.lower()


def test_module_level_expected_values_match_ansible() -> None:
    """Regression guard: the ansible task writes runtime=podman and
    runtime_version=1.  If either constant here changes without the
    task being updated (or vice versa), every existing host breaks.

    This test is deliberately not parameterised — it exists specifically
    to make the ``EXPECTED_RUNTIME`` / ``EXPECTED_RUNTIME_VERSION``
    values grep-findable from a failing test in CI, so the author of
    the mismatch sees this test die and remembers to update ``tasks
    /podman.yml`` in the same commit.
    """
    assert runtime_sentinel.EXPECTED_RUNTIME == "podman"
    assert runtime_sentinel.EXPECTED_RUNTIME_VERSION == 1
