"""Unit tests for compute_space.core.containers.

These tests mock ``subprocess`` so they run without a live podman daemon —
end-to-end tests that actually exercise podman live under the
``@requires_podman`` marker.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from compute_space.core import containers
from compute_space.core.containers import UID_MAP_BASE_START
from compute_space.core.containers import UID_MAP_RANGE_SIZE
from compute_space.core.containers import UID_MAP_WIDTH
from compute_space.core.containers import build_image
from compute_space.core.containers import compute_uid_map_base
from compute_space.core.containers import get_container_status
from compute_space.core.containers import remove_image
from compute_space.core.containers import run_container
from compute_space.core.containers import stop_container
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch, handler):
    """Replace subprocess.run globally with a callable that records + returns."""
    monkeypatch.setattr(subprocess, "run", handler)


def test_compute_uid_map_base_is_deterministic() -> None:
    """Same app id -> same subuid window, across calls."""
    assert compute_uid_map_base(1) == compute_uid_map_base(1)
    assert compute_uid_map_base(42) == compute_uid_map_base(42)


def test_compute_uid_map_base_windows_are_disjoint() -> None:
    """No two app ids share any overlapping UID range."""
    windows = [(compute_uid_map_base(i), compute_uid_map_base(i) + UID_MAP_WIDTH) for i in range(10)]
    # Adjacent pairs: end of N <= start of N+1 (touching is fine, overlapping isn't).
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        assert end <= start


def test_compute_uid_map_base_rejects_negative_ids() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compute_uid_map_base(-1)


def test_compute_uid_map_base_starts_at_the_configured_base() -> None:
    """id=0 maps to the base of the subuid range (matches ansible allocation)."""
    assert compute_uid_map_base(0) == UID_MAP_BASE_START


def test_compute_uid_map_base_rejects_ids_past_the_allocated_range() -> None:
    """AUTOINCREMENT ids never reuse slots, so the formula eventually
    exceeds the 10M subuid range allocated to host.  That must surface
    as a clear error, not a malformed --uidmap argument to podman."""
    # One past the last id that fits.
    overflow_id = UID_MAP_RANGE_SIZE // UID_MAP_WIDTH  # same as _MAX_APP_ID_FOR_UID_MAP + 1
    with pytest.raises(ValueError, match="subuid pool"):
        compute_uid_map_base(overflow_id)


# ---------------------------------------------------------------------------
# build_image
# ---------------------------------------------------------------------------


def test_build_image_uses_podman_build(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeCompleted(0, stdout="")

    _patch_subprocess_run(monkeypatch, fake_run)

    tag = build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert tag == "openhost-myapp:latest"
    assert calls[0] == [
        "podman",
        "build",
        "-t",
        "openhost-myapp:latest",
        "-f",
        "/tmp/repo/Dockerfile",
        "/tmp/repo",
    ]


def test_build_image_surfaces_cache_corrupt_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any output containing a known cache-corrupt fragment raises with the marker."""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            1,
            stderr="error: content digest sha256:deadbeef: not found",
        )

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert str(exc_info.value).startswith(containers.BUILD_CACHE_CORRUPT_MARKER)


def test_build_image_generic_failure_does_not_use_cache_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr="Dockerfile not found")

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    msg = str(exc_info.value)
    assert containers.BUILD_CACHE_CORRUPT_MARKER not in msg
    assert "Dockerfile not found" in msg


# ---------------------------------------------------------------------------
# run_container
# ---------------------------------------------------------------------------


def _basic_manifest(**overrides) -> AppManifest:  # type: ignore[no-untyped-def]
    kwargs = dict(
        name="myapp",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=256,
        cpu_millicores=500,
    )
    kwargs.update(overrides)
    return AppManifest(**kwargs)  # type: ignore[arg-type]


def _run_and_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest: AppManifest,
    tmp_path,
    port_mappings: list[PortMapping] | None = None,
    env_vars: dict[str, str] | None = None,
    uid_map_base: int = 10_000_000,
) -> list[str]:
    """Invoke run_container with mocked subprocess and return the built argv."""
    runs: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=60, **_):  # type: ignore[no-untyped-def]
        runs.append(list(cmd))
        # First call is the pre-run "podman rm -f" — no output needed.
        # Second call is the actual "podman run" — return a fake container id.
        if cmd[:2] == ["podman", "run"]:
            return _FakeCompleted(0, stdout="container-id-xyz\n")
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    data_dir = str(tmp_path / "persistent")
    temp_data_dir = str(tmp_path / "temp")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(temp_data_dir, exist_ok=True)
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", manifest.name), exist_ok=True)

    run_container(
        manifest.name,
        "openhost-myapp:latest",
        manifest,
        local_port=9001,
        env_vars=env_vars or {},
        data_dir=data_dir,
        temp_data_dir=temp_data_dir,
        uid_map_base=uid_map_base,
        port_mappings=port_mappings,
    )
    # The "run" call is the one after the pre-cleanup "rm".
    run_cmds = [c for c in runs if c[:2] == ["podman", "run"]]
    assert len(run_cmds) == 1
    return run_cmds[0]


def test_run_container_has_hardening_flags(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest()
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges=true" in argv
    assert "--add-host=host.docker.internal:host-gateway" in argv
    assert "--add-host=host.containers.internal:host-gateway" in argv


def test_run_container_maps_uidmap_and_gidmap(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest()
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path, uid_map_base=10_500_000)
    assert f"--uidmap=0:10500000:{UID_MAP_WIDTH}" in argv
    assert f"--gidmap=0:10500000:{UID_MAP_WIDTH}" in argv


def test_run_container_mounts_use_idmap_option(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest(app_data=True, app_temp_data=True, access_vm_data=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    # Every -v argument value should have :idmap (or :ro,idmap) as its
    # options suffix so container-root writes land on disk under the host
    # user, not under the mapped subuid.
    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert volume_args  # The test manifest requested three mounts.
    for v in volume_args:
        assert v.endswith(":idmap") or v.endswith(":ro,idmap"), v


def test_run_container_adds_manifest_capabilities(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest(capabilities=["NET_ADMIN", "NET_RAW"])
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    # --cap-drop=ALL must come before the --cap-add entries.
    drop_idx = argv.index("--cap-drop=ALL")
    for cap in ("NET_ADMIN", "NET_RAW"):
        pair_idx = argv.index(cap)
        assert argv[pair_idx - 1] == "--cap-add"
        assert pair_idx > drop_idx, "cap-drop must come before cap-add"


def test_run_container_access_all_data_mounts_parent_dirs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With access_all_data, the app sees /data/app_data/ and
    /data/app_temp_data/ as whole-namespace parent mounts, not just its
    own subdir.  This is security-sensitive because a typo here would
    expose every app's data to every app; pin the exact mount layout."""
    manifest = _basic_manifest(access_all_data=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    # Every mount must be idmap (rw or ro).
    for v in volume_args:
        assert v.endswith(":idmap") or v.endswith(":ro,idmap"), v

    # Specifically, the app_data/app_temp_data parent mounts must be the
    # *parent* directories, not the per-app subdirectories.
    targets = [v.rsplit(":", 2)[1] for v in volume_args]
    assert "/data/app_data" in targets
    assert "/data/app_temp_data" in targets
    # vm_data is still mounted rw when access_all_data is on.
    assert "/data/vm_data" in targets


def test_run_container_port_mappings_bind_tcp_and_udp_on_all_interfaces(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifests with [[ports]] should publish TCP and UDP on 0.0.0.0 for
    every entry.  Port mappings are user-visible and binding to the wrong
    interface or protocol would silently break apps."""
    manifest = _basic_manifest()
    argv = _run_and_capture(
        monkeypatch,
        manifest=manifest,
        tmp_path=tmp_path,
        port_mappings=[
            PortMapping(label="wg", container_port=51820, host_port=51820),
            PortMapping(label="dns", container_port=5300, host_port=5300),
        ],
    )

    # Collect every -p value.
    p_values = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-p"]
    # Two mappings -> 4 extra -p entries (tcp+udp each), plus the main HTTP
    # port mapping that run_container always adds (127.0.0.1:local_port:ctnr).
    assert "0.0.0.0:51820:51820/tcp" in p_values
    assert "0.0.0.0:51820:51820/udp" in p_values
    assert "0.0.0.0:5300:5300/tcp" in p_values
    assert "0.0.0.0:5300:5300/udp" in p_values


# ---------------------------------------------------------------------------
# stop, remove, status, cache drop
# ---------------------------------------------------------------------------


def test_stop_container_calls_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    stop_container("abc123")
    assert calls == [
        ["podman", "stop", "abc123"],
        ["podman", "rm", "-f", "abc123"],
    ]


def test_remove_image_calls_podman_rmi(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    remove_image("myapp")
    assert calls == [["podman", "rmi", "openhost-myapp:latest"]]


def test_get_container_status_returns_unknown_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr="no such container")

    _patch_subprocess_run(monkeypatch, fake_run)
    assert get_container_status("bogus") == "unknown"


# NOTE: build-cache drop is exercised in test_build_cache.py with the
# same subprocess mock plus kwarg assertions.  Not duplicated here.
