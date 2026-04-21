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
from compute_space.core.containers import _translate_env_for_container
from compute_space.core.containers import build_image
from compute_space.core.containers import build_log_path
from compute_space.core.containers import compute_uid_map_base
from compute_space.core.containers import get_app_logs
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


@pytest.mark.parametrize(
    "fragment",
    [
        "error: content digest sha256:deadbeef: not found",
        "Error: storage-driver errored: something happened",
        "Error: layer not known: sha256:whatever",
    ],
)
def test_build_image_detects_every_known_cache_corrupt_fragment(
    monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """Every substring in _BUILD_CACHE_CORRUPT_FRAGMENTS must trigger the
    BUILD_CACHE_CORRUPT marker via the non-streaming build code path
    (no temp_data_dir) so the dashboard's 'drop cache' toast offers a
    remediation, not a blind retry."""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr=fragment)

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert str(exc_info.value).startswith(containers.BUILD_CACHE_CORRUPT_MARKER)


@pytest.mark.parametrize(
    "fragment",
    [
        "error: content digest sha256:deadbeef: not found",
        "Error: storage-driver errored: something happened",
        "Error: layer not known: sha256:whatever",
    ],
)
def test_build_image_streaming_path_detects_cache_corrupt(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """The streaming build path (temp_data_dir set) assembles build_output
    incrementally from Popen.stdout and then runs the same substring check.
    It needs its own regression coverage because the non-streaming code
    path uses a different subprocess API.
    """

    class _FakePopen:
        def __init__(self, *a, **_kw):  # type: ignore[no-untyped-def]
            # Every line of the "build" ends with a newline; the code under
            # test iterates over proc.stdout and appends to build_output.
            self.stdout = iter([fragment + "\n"])
            self.returncode = 1

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    monkeypatch.setattr("compute_space.core.containers.subprocess.Popen", _FakePopen)

    temp_data_dir = str(tmp_path / "temp")
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", "myapp"), exist_ok=True)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=temp_data_dir)
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


# ---------------------------------------------------------------------------
# _translate_env_for_container
# ---------------------------------------------------------------------------


def test_translate_env_rewrites_openhost_app_data_dir_to_container_path() -> None:
    """The router hands the host path; inside the container the app needs
    the mount-target path.  A subtle mistake here silently breaks every
    app that reads OPENHOST_APP_DATA_DIR."""
    translated = _translate_env_for_container(
        {"OPENHOST_APP_DATA_DIR": "/host/persistent_data/app_data/notes"},
        app_name="notes",
        app_data_dir="/host/persistent_data/app_data/notes",
    )
    assert translated["OPENHOST_APP_DATA_DIR"] == "/data/app_data/notes"


def test_translate_env_rewrites_openhost_app_temp_dir_to_container_path() -> None:
    translated = _translate_env_for_container(
        {"OPENHOST_APP_TEMP_DIR": "/host/temporary_data/app_temp_data/notes"},
        app_name="notes",
        app_data_dir="/host/persistent_data/app_data/notes",
    )
    assert translated["OPENHOST_APP_TEMP_DIR"] == "/data/app_temp_data/notes"


def test_translate_env_rewrites_openhost_sqlite_paths_relative_to_app_data() -> None:
    """SQLite db paths live under app_data_dir; the rewrite must preserve
    the sub-path (e.g. sqlite/main.db) under the container-side mount."""
    translated = _translate_env_for_container(
        {
            "OPENHOST_SQLITE_main": "/host/persistent_data/app_data/notes/sqlite/main.db",
            "OPENHOST_SQLITE_cache": "/host/persistent_data/app_data/notes/sqlite/cache.db",
        },
        app_name="notes",
        app_data_dir="/host/persistent_data/app_data/notes",
    )
    assert translated["OPENHOST_SQLITE_main"] == "/data/app_data/notes/sqlite/main.db"
    assert translated["OPENHOST_SQLITE_cache"] == "/data/app_data/notes/sqlite/cache.db"


def test_translate_env_passes_unrelated_keys_through_unchanged() -> None:
    """Only OpenHost-controlled paths get rewritten; everything else must
    reach the container untouched."""
    translated = _translate_env_for_container(
        {
            "OPENHOST_APP_NAME": "notes",
            "OPENHOST_ZONE_DOMAIN": "example.local",
            "MY_APP_TOKEN": "secret-token",
        },
        app_name="notes",
        app_data_dir="/host/app_data",
    )
    assert translated["OPENHOST_APP_NAME"] == "notes"
    assert translated["OPENHOST_ZONE_DOMAIN"] == "example.local"
    assert translated["MY_APP_TOKEN"] == "secret-token"


# ---------------------------------------------------------------------------
# run_container
# ---------------------------------------------------------------------------


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
    # With access_all_data, vm_data is explicitly mounted rw.  Pin this —
    # silently swapping it to ro would break every app that relies on
    # /data/vm_data as shared scratch space.
    vm_mount = next(v for v in volume_args if v.endswith(":/data/vm_data:idmap"))
    assert "ro" not in vm_mount.rsplit(":", 1)[1], f"vm_data should be rw under access_all_data, got {vm_mount}"


def test_run_container_access_vm_data_mounts_vm_data_read_only(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """access_vm_data (without access_all_data) grants only a read-only
    view of /data/vm_data.  The distinction matters: this is what lets
    apps read shared state without being able to corrupt it, and a
    regression swapping in rw here would turn a security-critical mount
    into a write-through channel.
    """
    manifest = _basic_manifest(access_vm_data=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    vm_mounts = [v for v in volume_args if "/data/vm_data" in v]
    assert len(vm_mounts) == 1, vm_mounts
    assert vm_mounts[0].endswith(":ro,idmap"), vm_mounts[0]


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
    # The main loopback-only HTTP mapping must always be present, and the
    # two [[ports]] entries must each expand to a tcp + udp publish on
    # 0.0.0.0.
    assert "127.0.0.1:9001:8080" in p_values
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


# ---------------------------------------------------------------------------
# get_app_logs
# ---------------------------------------------------------------------------


def test_get_app_logs_returns_build_log_when_no_container(tmp_path) -> None:
    """When the app has no running container, get_app_logs should still
    return the build log contents (and skip the podman logs call)."""
    log_file = build_log_path("notes", str(tmp_path))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as f:
        f.write("build-line-1\nbuild-line-2\n")

    output = get_app_logs("notes", str(tmp_path), container_id=None)
    assert "build-line-1" in output
    assert "build-line-2" in output
    # No "Container logs" header should appear since no container id.
    assert "=== Container logs ===" not in output


def test_get_app_logs_appends_podman_logs_when_container_id_set(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a container id, get_app_logs should also run ``podman logs
    --tail <N>`` and concatenate the output under a 'Container logs' header."""
    log_file = build_log_path("notes", str(tmp_path))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as f:
        f.write("=== Build complete ===\n")

    captured: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=10, **_):  # type: ignore[no-untyped-def]
        captured.append(list(cmd))
        return _FakeCompleted(0, stdout="app stdout line\n")

    _patch_subprocess_run(monkeypatch, fake_run)

    output = get_app_logs("notes", str(tmp_path), container_id="abc123", tail=500)

    assert captured == [["podman", "logs", "--tail", "500", "abc123"]]
    assert "=== Build complete ===" in output
    assert "=== Container logs ===" in output
    assert "app stdout line" in output


def test_get_app_logs_falls_back_to_legacy_docker_log_filename(tmp_path) -> None:
    """Deployments whose build log was written under the pre-rename
    filename (``docker.log``) must still show up in the dashboard until
    the next rebuild switches them to ``build.log``.  Exercise that
    backward-compat read path."""
    app_temp = os.path.join(str(tmp_path), "app_temp_data", "notes")
    os.makedirs(app_temp, exist_ok=True)
    with open(os.path.join(app_temp, "docker.log"), "w") as f:
        f.write("legacy build output\n")

    output = get_app_logs("notes", str(tmp_path), container_id=None)
    assert "legacy build output" in output


def test_get_app_logs_prefers_build_log_over_legacy_name(tmp_path) -> None:
    """When both filenames exist (we shouldn't normally see this, but a
    half-migrated deployment can), the current filename wins."""
    app_temp = os.path.join(str(tmp_path), "app_temp_data", "notes")
    os.makedirs(app_temp, exist_ok=True)
    with open(os.path.join(app_temp, "docker.log"), "w") as f:
        f.write("stale legacy content\n")
    with open(os.path.join(app_temp, "build.log"), "w") as f:
        f.write("fresh build content\n")

    output = get_app_logs("notes", str(tmp_path), container_id=None)
    assert "fresh build content" in output
    assert "stale legacy content" not in output


def test_get_app_logs_strips_ansi_escape_sequences(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Color codes and carriage returns from podman logs should be
    stripped before we hand the text back to the dashboard (which
    otherwise renders them as garbage text)."""
    log_file = build_log_path("notes", str(tmp_path))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as f:
        f.write("")

    def fake_run(cmd, capture_output=False, text=False, timeout=10, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            0,
            stdout="\x1b[31mred text\x1b[0m plain\r\n",
        )

    _patch_subprocess_run(monkeypatch, fake_run)

    output = get_app_logs("notes", str(tmp_path), container_id="abc123")
    assert "\x1b[" not in output
    assert "\r" not in output
    assert "red text plain" in output
