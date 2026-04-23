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
