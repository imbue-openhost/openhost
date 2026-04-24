"""Tests for compute_space.core.runtime_sentinel.

The sentinel lets the dashboard warn the operator that clicking Update
would land them on a router whose runtime prerequisites aren't met by
the host.  It is deliberately NOT a hard startup gate — the runtime
probe in core.containers.container_runtime_available covers the initial Docker
→ podman transition where pre-upgrade code has no sentinel knowledge.

These tests exercise every branch of the parser and host_prep_status
against a sentinel written to tmp_path rather than the real /etc.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path

import pytest

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.core import runtime_sentinel
from compute_space.core.manifest import UNPRIVILEGED_PORT_FLOOR
from compute_space.core.runtime_sentinel import EXPECTED_RUNTIME
from compute_space.core.runtime_sentinel import EXPECTED_RUNTIME_VERSION
from compute_space.core.runtime_sentinel import host_prep_status


def _write_sentinel(path: Path, runtime: str, version: str) -> None:
    path.write_text(f"runtime={runtime}\nruntime_version={version}\n")


def test_matching_sentinel_is_ok(tmp_path: Path) -> None:
    sentinel = tmp_path / "runtime"
    _write_sentinel(sentinel, EXPECTED_RUNTIME, str(EXPECTED_RUNTIME_VERSION))

    status = host_prep_status(str(sentinel))
    assert status.ok is True
    assert status.reason == ""


def test_missing_sentinel_is_not_ok(tmp_path: Path) -> None:
    """The most common failure mode: Docker-era host with no /etc/openhost."""
    sentinel = tmp_path / "does-not-exist"

    status = host_prep_status(str(sentinel))
    assert status.ok is False
    assert status.reason == "missing"
    assert "ansible" in status.message.lower()


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
    about.  Unknown keys must be silently ignored."""
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
    task being updated (or vice versa), existing hosts see a banner
    on next upgrade-check.

    This test is deliberately not parameterised — it exists specifically
    to make the ``EXPECTED_RUNTIME`` / ``EXPECTED_RUNTIME_VERSION``
    values grep-findable from a failing test in CI, so the author of
    a mismatch sees this test die and remembers to update ``tasks
    /podman.yml`` in the same commit.
    """
    assert runtime_sentinel.EXPECTED_RUNTIME == "podman"
    assert runtime_sentinel.EXPECTED_RUNTIME_VERSION == 1


# ---------------------------------------------------------------------------
# Cross-check Python constants against the ansible tasks that set them.
#
# The existing `test_module_level_expected_values_match_ansible` only
# asserts the Python side: bumping EXPECTED_RUNTIME_VERSION in Python
# without updating ansible/tasks/podman.yml (or vice versa) passes that
# test but ships a silently-broken host-prep — every freshly provisioned
# host sees a "host not prepped" banner forever because the sentinel
# value written by ansible no longer matches what the router expects.
#
# Same story for UNPRIVILEGED_PORT_FLOOR vs the sysctl value the task
# writes to /etc/sysctl.d/90-openhost-podman.conf.
# ---------------------------------------------------------------------------


_PODMAN_TASKS_PATH = OPENHOST_PROJECT_DIR / "ansible" / "tasks" / "podman.yml"


def _read_podman_task_text() -> str:
    """Read the ansible task file verbatim.  We parse with regex rather
    than a YAML loader because the values we care about are inline
    literals inside ``content: |`` blocks — structural YAML awareness
    doesn't help and just couples the test to PyYAML."""
    return _PODMAN_TASKS_PATH.read_text()


def test_ansible_podman_task_file_is_present() -> None:
    """Guard against the tasks file being moved or renamed without
    this test being updated — otherwise the two asserts below would
    silently skip."""
    assert _PODMAN_TASKS_PATH.is_file(), (
        f"expected ansible task file at {_PODMAN_TASKS_PATH}; if you moved it, update _PODMAN_TASKS_PATH in this test"
    )


def test_ansible_writes_matching_runtime_version() -> None:
    """The ansible task writes ``runtime_version=<N>`` to the sentinel
    file.  That N must match ``EXPECTED_RUNTIME_VERSION`` or the
    dashboard nags the operator on every update check even after a
    fresh playbook run.

    Parses the file as text so we aren't depending on YAML semantics —
    the literal is what ends up on disk regardless of how the
    surrounding YAML is structured."""
    text = _read_podman_task_text()
    matches = re.findall(r"runtime_version=(\d+)", text)
    assert matches, (
        f"no ``runtime_version=<N>`` literal found in {_PODMAN_TASKS_PATH}; did the sentinel-write task change?"
    )
    # Every occurrence must match; if the task writes the value in
    # more than one place they must all agree.
    distinct = set(matches)
    assert len(distinct) == 1, f"ansible task writes inconsistent runtime_version values: {distinct}"
    written = int(next(iter(distinct)))
    assert written == EXPECTED_RUNTIME_VERSION, (
        f"ansible writes runtime_version={written} but Python expects "
        f"EXPECTED_RUNTIME_VERSION={EXPECTED_RUNTIME_VERSION}; bump both "
        "or hosts will see a stale-sentinel banner after re-provisioning"
    )


def test_ansible_writes_matching_runtime_name() -> None:
    """Symmetric check for the ``runtime=`` literal — catches a hypothetical
    future rename from podman to something else that forgets to update
    ansible."""
    text = _read_podman_task_text()
    matches = re.findall(r"(?m)^\s*runtime=(\w+)\s*$", text)
    assert matches, f"no ``runtime=<name>`` literal found in {_PODMAN_TASKS_PATH}; did the sentinel-write task change?"
    distinct = set(matches)
    assert len(distinct) == 1, f"ansible task writes inconsistent runtime values: {distinct}"
    written = next(iter(distinct))
    assert written == EXPECTED_RUNTIME, (
        f"ansible writes runtime={written} but Python expects EXPECTED_RUNTIME={EXPECTED_RUNTIME!r}"
    )


def test_ansible_unprivileged_port_floor_matches_python_constant() -> None:
    """``ansible/tasks/podman.yml`` writes a sysctl
    ``net.ipv4.ip_unprivileged_port_start = <N>`` drop-in.  That N
    must equal ``UNPRIVILEGED_PORT_FLOOR`` (the manifest-parse
    boundary) or apps get rejected at parse time for perfectly valid
    ports the host kernel would otherwise accept — or worse, rejected
    at bind time for ports the manifest layer wrongly accepted."""
    text = _read_podman_task_text()
    matches = re.findall(
        r"net\.ipv4\.ip_unprivileged_port_start\s*=\s*(\d+)",
        text,
    )
    assert matches, (
        "no ``net.ipv4.ip_unprivileged_port_start`` sysctl literal "
        f"found in {_PODMAN_TASKS_PATH}; did the sysctl-drop-in task change?"
    )
    distinct = set(matches)
    assert len(distinct) == 1, f"ansible writes inconsistent port floor values: {distinct}"
    written = int(next(iter(distinct)))
    assert written == UNPRIVILEGED_PORT_FLOOR, (
        f"ansible writes net.ipv4.ip_unprivileged_port_start={written} "
        f"but Python's UNPRIVILEGED_PORT_FLOOR is {UNPRIVILEGED_PORT_FLOOR}; "
        "bump both or manifests will silently disagree with the kernel"
    )
