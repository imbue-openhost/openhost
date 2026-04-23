"""Unit tests for the container data-mount resolver.

Exercises the permission-resolution rules in
``compute_space.core.containers.compute_data_mounts`` without actually
running Docker. These are complementary to ``test_manifest.py``, which
covers the manifest fields in isolation.
"""

from __future__ import annotations

import pytest

from compute_space.core.containers import compute_data_mounts
from compute_space.core.manifest import parse_manifest_from_string

MINIMAL = """\
[app]
name = "testapp"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


def _mounts(**flags):
    """Build a manifest from flags and return the resolved mounts list."""
    lines = ["[data]"]
    for k, v in flags.items():
        if isinstance(v, list):
            lines.append(f"{k} = {v!r}".replace("'", '"'))
        else:
            lines.append(f"{k} = {str(v).lower()}")
    toml = MINIMAL + "\n" + "\n".join(lines) + "\n"
    manifest = parse_manifest_from_string(toml)
    return compute_data_mounts(
        manifest, "testapp", data_dir="/host/data", temp_data_dir="/host/temp"
    )


class TestDefaultNoAccess:
    def test_no_data_section_produces_no_mounts(self):
        manifest = parse_manifest_from_string(MINIMAL)
        mounts = compute_data_mounts(manifest, "testapp", "/host/data", "/host/temp")
        assert mounts == []


class TestScopedMounts:
    def test_app_data_only(self):
        mounts = _mounts(app_data=True)
        assert mounts == [
            ("/host/data/app_data/testapp", "/data/app_data/testapp", None)
        ]

    def test_app_temp_data_only(self):
        mounts = _mounts(app_temp_data=True)
        assert mounts == [
            ("/host/temp/app_temp_data/testapp", "/data/app_temp_data/testapp", None)
        ]

    def test_both_scoped(self):
        mounts = _mounts(app_data=True, app_temp_data=True)
        assert mounts == [
            ("/host/data/app_data/testapp", "/data/app_data/testapp", None),
            ("/host/temp/app_temp_data/testapp", "/data/app_temp_data/testapp", None),
        ]


class TestVmDataMounts:
    def test_vm_data_ro(self):
        mounts = _mounts(access_vm_data=True)
        assert mounts == [("/host/data/vm_data", "/data/vm_data", "ro")]

    def test_vm_data_rw(self):
        mounts = _mounts(access_vm_data_rw=True)
        assert mounts == [("/host/data/vm_data", "/data/vm_data", None)]


class TestBroadMounts:
    def test_all_apps_data_only(self):
        mounts = _mounts(access_all_apps_data=True)
        # Parent mount; no scoped mount, no temp, no vm_data.
        assert mounts == [("/host/data/app_data", "/data/app_data", None)]

    def test_all_apps_data_shadows_scoped(self):
        # Requesting both broad + scoped results in only the broad mount
        # (the scoped path would be shadowed anyway).
        mounts = _mounts(access_all_apps_data=True, app_data=True)
        assert mounts == [("/host/data/app_data", "/data/app_data", None)]

    def test_all_apps_temp_data_only(self):
        mounts = _mounts(access_all_apps_temp_data=True)
        assert mounts == [
            ("/host/temp/app_temp_data", "/data/app_temp_data", None)
        ]

    def test_all_apps_temp_data_shadows_scoped(self):
        # Symmetric to the app_data shadowing test above.
        mounts = _mounts(access_all_apps_temp_data=True, app_temp_data=True)
        assert mounts == [
            ("/host/temp/app_temp_data", "/data/app_temp_data", None)
        ]


class TestSqliteImpliesAppData:
    def test_sqlite_alone_enables_scoped_mount(self):
        # Requesting ``sqlite`` entries is a shorthand for ``app_data``;
        # it must produce the same scoped mount as requesting app_data
        # directly.
        mounts = _mounts(sqlite=["main"])
        assert mounts == [
            ("/host/data/app_data/testapp", "/data/app_data/testapp", None)
        ]


class TestIndependentCombinations:
    def test_all_apps_data_plus_vm_ro(self):
        # Classic "inspect the host" permission set: see every app's
        # permanent data + read-only vm_data, nothing else.
        mounts = _mounts(access_all_apps_data=True, access_vm_data=True)
        assert mounts == [
            ("/host/data/app_data", "/data/app_data", None),
            ("/host/data/vm_data", "/data/vm_data", "ro"),
        ]

    def test_all_apps_temp_data_plus_vm_rw(self):
        mounts = _mounts(
            access_all_apps_temp_data=True, access_vm_data_rw=True
        )
        assert mounts == [
            ("/host/temp/app_temp_data", "/data/app_temp_data", None),
            ("/host/data/vm_data", "/data/vm_data", None),
        ]

    def test_all_three_broad(self):
        mounts = _mounts(
            access_all_apps_data=True,
            access_all_apps_temp_data=True,
            access_vm_data_rw=True,
        )
        assert mounts == [
            ("/host/data/app_data", "/data/app_data", None),
            ("/host/temp/app_temp_data", "/data/app_temp_data", None),
            ("/host/data/vm_data", "/data/vm_data", None),
        ]


class TestLegacyAccessAllData:
    def test_access_all_data_equivalent_to_all_three_broad(self):
        # The legacy shorthand should produce the same mount set as
        # requesting all three fine-grained flags independently.
        legacy = _mounts(access_all_data=True)
        fine = _mounts(
            access_all_apps_data=True,
            access_all_apps_temp_data=True,
            access_vm_data_rw=True,
        )
        assert legacy == fine


class TestOpenhostStateMount:
    def test_access_openhost_state_ro_alone(self):
        # Asking for router-state access only — no data mounts at all.
        mounts = _mounts(access_openhost_state_ro=True)
        assert mounts == [("/host/data/openhost", "/data/openhost", "ro")]

    def test_access_openhost_state_ro_with_access_all_data(self):
        # Stacks cleanly on top of the legacy shorthand. Order: the
        # three data-category mounts first, then openhost last.
        mounts = _mounts(access_all_data=True, access_openhost_state_ro=True)
        assert mounts == [
            ("/host/data/app_data", "/data/app_data", None),
            ("/host/temp/app_temp_data", "/data/app_temp_data", None),
            ("/host/data/vm_data", "/data/vm_data", None),
            ("/host/data/openhost", "/data/openhost", "ro"),
        ]

    def test_access_all_data_alone_does_not_mount_openhost_state(self):
        # Guarantees the backward-compat invariant at the mount layer:
        # existing manifests using only access_all_data must never
        # expose /data/openhost.
        mounts = _mounts(access_all_data=True)
        assert all(m[1] != "/data/openhost" for m in mounts)

    def test_openhost_state_host_path_helper_matches_mount(self):
        # The host-side path for the router state dir is computed in
        # two places (compute_data_mounts emits it into the mount
        # tuple; run_container uses it as a sentinel for the
        # no-autocreate guard). The shared helper exists specifically
        # so these two places can never drift — pin that here.
        from compute_space.core.containers import _openhost_state_host_path

        mounts = _mounts(access_openhost_state_ro=True)
        assert mounts[0][0] == _openhost_state_host_path("/host/data")


class TestEnsureMountHostDirs:
    """``_ensure_mount_host_dirs`` creates all requested host paths
    except the router's own state dir, which is intentionally left
    alone so a missing router dir fails loudly rather than silently
    producing an empty backup."""

    def test_creates_app_data_and_vm_data_dirs(self, tmp_path):
        from compute_space.core.containers import _ensure_mount_host_dirs

        data_dir = str(tmp_path / "data")
        temp_dir = str(tmp_path / "temp")
        mounts = [
            (f"{data_dir}/app_data/testapp", "/data/app_data/testapp", None),
            (f"{data_dir}/vm_data", "/data/vm_data", None),
            (f"{temp_dir}/app_temp_data/testapp", "/data/app_temp_data/testapp", None),
        ]
        _ensure_mount_host_dirs(mounts, data_dir)
        import os as _os

        for host, _c, _o in mounts:
            assert _os.path.isdir(host), f"{host} should have been created"

    def test_does_not_create_openhost_state_dir(self, tmp_path):
        """Even if the openhost mount is requested, the host dir
        is not auto-created. Docker will fail the bind-mount on its
        own if the dir is missing — that's the intended behaviour."""
        from compute_space.core.containers import _ensure_mount_host_dirs

        data_dir = str(tmp_path / "data")
        mounts = [
            (f"{data_dir}/openhost", "/data/openhost", "ro"),
        ]
        _ensure_mount_host_dirs(mounts, data_dir)
        import os as _os

        # Still absent.
        assert not _os.path.exists(f"{data_dir}/openhost")

    def test_creates_others_even_if_openhost_requested_too(self, tmp_path):
        from compute_space.core.containers import _ensure_mount_host_dirs

        data_dir = str(tmp_path / "data")
        mounts = [
            (f"{data_dir}/app_data", "/data/app_data", None),
            (f"{data_dir}/vm_data", "/data/vm_data", None),
            (f"{data_dir}/openhost", "/data/openhost", "ro"),
        ]
        _ensure_mount_host_dirs(mounts, data_dir)
        import os as _os

        assert _os.path.isdir(f"{data_dir}/app_data")
        assert _os.path.isdir(f"{data_dir}/vm_data")
        # openhost specifically skipped.
        assert not _os.path.exists(f"{data_dir}/openhost")


class TestManifestRejection:
    def test_vm_data_ro_and_rw_rejected_at_parse_time(self):
        toml = MINIMAL + "\n[data]\naccess_vm_data = true\naccess_vm_data_rw = true\n"
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_manifest_from_string(toml)

    def test_vm_data_ro_plus_access_all_data_rejected_at_parse_time(self):
        # Parse-time contradiction even though ``access_all_data`` grants
        # vm_data via a different flag (implicit RW).
        toml = MINIMAL + "\n[data]\naccess_vm_data = true\naccess_all_data = true\n"
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_manifest_from_string(toml)
