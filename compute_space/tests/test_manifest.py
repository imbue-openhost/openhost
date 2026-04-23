"""Unit tests for the openhost.toml manifest parser."""

import dataclasses
import json

import pytest

from compute_space.core.manifest import parse_manifest_from_string

MINIMAL = """\
[app]
name = "test-app"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


class TestDefaults:
    """Verify default values match the documented manifest spec."""

    def test_cpu_millicores_default_is_100(self):
        """cpu_millicores should default to 100 when omitted (manifest_spec.md)."""
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.cpu_millicores == 100

    def test_memory_mb_default_is_128(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.memory_mb == 128

    def test_gpu_default_is_false(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.gpu is False

    def test_public_paths_default_is_empty(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.public_paths == []

    def test_hidden_default_is_false(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.hidden is False

    def test_data_flags_default_to_false(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.app_data is False
        assert manifest.app_temp_data is False
        assert manifest.access_vm_data is False
        assert manifest.access_all_data is False
        assert manifest.access_all_apps_data is False
        assert manifest.access_all_apps_temp_data is False
        assert manifest.access_vm_data_rw is False

    def test_sqlite_default_empty(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.sqlite_dbs == []

    def test_container_extra_fields_default_empty(self):
        """capabilities, devices, and port_mappings default to []."""
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.port_mappings == []
        assert manifest.capabilities == []
        assert manifest.devices == []

    def test_runtime_type_defaults(self):
        """When [runtime] type is omitted, it defaults correctly."""
        toml = """\
[app]
name = "test-app"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""
        manifest = parse_manifest_from_string(toml)
        assert manifest.runtime_type == "serverfull"
        assert manifest.container_image == "Dockerfile"
        assert manifest.container_port == 8080


class TestExplicitValues:
    """Verify that explicitly set values override defaults."""

    def test_cpu_millicores_explicit(self):
        toml = MINIMAL + "\n[resources]\ncpu_millicores = 500\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.cpu_millicores == 500

    def test_memory_mb_explicit(self):
        toml = MINIMAL + "\n[resources]\nmemory_mb = 256\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.memory_mb == 256

    def test_public_paths_explicit(self):
        toml = MINIMAL + '\n[routing]\npublic_paths = ["/api"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.public_paths == ["/api"]

    def test_hidden_explicit_true(self):
        toml = MINIMAL.replace('version = "0.1.0"', 'version = "0.1.0"\nhidden = true')
        manifest = parse_manifest_from_string(toml)
        assert manifest.hidden is True

    def test_hidden_explicit_false(self):
        toml = MINIMAL.replace('version = "0.1.0"', 'version = "0.1.0"\nhidden = false')
        manifest = parse_manifest_from_string(toml)
        assert manifest.hidden is False


class TestPortMappings:
    """Verify [[ports]] parsing."""

    def test_single_port_mapping(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "metrics"\ncontainer_port = 9090\nhost_port = 9090\n'
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.port_mappings) == 1
        pm = manifest.port_mappings[0]
        assert pm.label == "metrics"
        assert pm.container_port == 9090
        assert pm.host_port == 9090

    def test_multiple_port_mappings(self):
        toml = (
            MINIMAL
            + """
[[ports]]
label = "metrics"
container_port = 9090
host_port = 9090

[[ports]]
label = "debug"
container_port = 5005
host_port = 0
"""
        )
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.port_mappings) == 2
        assert manifest.port_mappings[0].label == "metrics"
        assert manifest.port_mappings[1].label == "debug"
        assert manifest.port_mappings[1].host_port == 0

    def test_host_port_defaults_to_zero(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "auto"\ncontainer_port = 3000\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings[0].host_port == 0

    def test_duplicate_label_raises(self):
        toml = (
            MINIMAL
            + """
[[ports]]
label = "dup"
container_port = 3000

[[ports]]
label = "dup"
container_port = 4000
"""
        )
        with pytest.raises(ValueError, match="Duplicate port label"):
            parse_manifest_from_string(toml)

    def test_missing_label_raises(self):
        toml = MINIMAL + "\n[[ports]]\ncontainer_port = 3000\n"
        with pytest.raises(ValueError, match="label"):
            parse_manifest_from_string(toml)

    def test_missing_container_port_raises(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "test"\n'
        with pytest.raises(ValueError, match="container_port"):
            parse_manifest_from_string(toml)

    def test_container_port_zero_accepted(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "ephemeral"\ncontainer_port = 0\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings[0].container_port == 0

    def test_negative_container_port_raises(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "bad"\ncontainer_port = -1\n'
        with pytest.raises(ValueError, match="container_port"):
            parse_manifest_from_string(toml)

    def test_negative_host_port_raises(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "test"\ncontainer_port = 80\nhost_port = -1\n'
        with pytest.raises(ValueError, match="host_port"):
            parse_manifest_from_string(toml)

    def test_duplicate_container_port_raises(self):
        toml = (
            MINIMAL
            + """
[[ports]]
label = "a"
container_port = 3000

[[ports]]
label = "b"
container_port = 3000
"""
        )
        with pytest.raises(ValueError, match="Duplicate container_port 3000"):
            parse_manifest_from_string(toml)

    def test_duplicate_host_port_raises(self):
        toml = (
            MINIMAL
            + """
[[ports]]
label = "a"
container_port = 3000
host_port = 9090

[[ports]]
label = "b"
container_port = 4000
host_port = 9090
"""
        )
        with pytest.raises(ValueError, match="Duplicate host_port 9090"):
            parse_manifest_from_string(toml)

    def test_duplicate_host_port_zero_allowed(self):
        """Multiple host_port=0 (auto-assign) is fine."""
        toml = (
            MINIMAL
            + """
[[ports]]
label = "a"
container_port = 3000
host_port = 0

[[ports]]
label = "b"
container_port = 4000
host_port = 0
"""
        )
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.port_mappings) == 2

    def test_extra_ports_deprecation_warns(self):
        """Deprecated extra_ports logs warning but is otherwise ignored."""
        toml = MINIMAL + 'extra_ports = ["8081:8081"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings == []

    def test_port_mappings_and_extra_ports_coexist(self):
        toml = MINIMAL + 'extra_ports = ["8081:8081"]\n\n[[ports]]\nlabel = "metrics"\ncontainer_port = 9090\n'
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.port_mappings) == 1

    def test_manifest_with_port_mappings_is_json_serializable(self):
        """Regression: manifests with [[ports]] must round-trip through
        ``dataclasses.asdict`` + ``json.dumps`` so that
        ``/api/clone_and_get_app_info`` can return them. Previously
        ``PortMapping`` was an attrs class, which ``dataclasses.asdict``
        leaves as-is, and Flask's default JSON encoder then raises
        ``TypeError: Object of type PortMapping is not JSON serializable``.
        """
        toml = (
            MINIMAL
            + """
[[ports]]
label = "metrics"
container_port = 9090
host_port = 9090

[[ports]]
label = "debug"
container_port = 5005
host_port = 0
"""
        )
        manifest = parse_manifest_from_string(toml)
        info = dataclasses.asdict(manifest)
        # Round-trip through JSON; will raise TypeError on regression.
        payload = json.dumps(info)
        decoded = json.loads(payload)
        assert decoded["port_mappings"] == [
            {"label": "metrics", "container_port": 9090, "host_port": 9090},
            {"label": "debug", "container_port": 5005, "host_port": 0},
        ]


class TestContainerParsing:
    """Verify container fields are parsed correctly."""

    def test_container_fields(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.container_image == "Dockerfile"
        assert manifest.container_port == 8080
        assert manifest.container_command is None

    def test_container_command(self):
        toml = MINIMAL + 'command = "/data -A"\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.container_command == "/data -A"

    def test_extra_ports_deprecated(self):
        """extra_ports is deprecated; still parses without error but field removed."""
        toml = MINIMAL + 'extra_ports = ["8081:8081"]\n'
        manifest = parse_manifest_from_string(toml)
        assert not hasattr(manifest, "extra_ports")

    def test_capabilities(self):
        toml = MINIMAL + 'capabilities = ["NET_ADMIN"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.capabilities == ["NET_ADMIN"]

    def test_devices(self):
        toml = MINIMAL + 'devices = ["/dev/tun"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.devices == ["/dev/tun"]


class TestValidation:
    """Verify that invalid manifests raise errors."""

    def test_missing_app_name(self):
        toml = '[app]\nversion = "0.1.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 80\n'
        with pytest.raises(ValueError, match="name"):
            parse_manifest_from_string(toml)

    def test_missing_app_version(self):
        toml = '[app]\nname = "x"\n[runtime.container]\nimage = "Dockerfile"\nport = 80\n'
        with pytest.raises(ValueError, match="version"):
            parse_manifest_from_string(toml)

    def test_invalid_runtime_type(self):
        toml = '[app]\nname = "x"\nversion = "1"\n[runtime]\ntype = "invalid"\n'
        with pytest.raises(ValueError, match="Invalid runtime type"):
            parse_manifest_from_string(toml)

    def test_missing_image(self):
        toml = '[app]\nname = "x"\nversion = "1"\n[runtime.container]\nport = 80\n'
        with pytest.raises(ValueError, match="image"):
            parse_manifest_from_string(toml)

    def test_missing_port(self):
        toml = '[app]\nname = "x"\nversion = "1"\n[runtime.container]\nimage = "Dockerfile"\n'
        with pytest.raises(ValueError, match="port"):
            parse_manifest_from_string(toml)


class TestFineGrainedDataAccess:
    """Verify the independently-requestable data access flags."""

    def _with_data(self, **flags):
        """Build a minimal manifest with a [data] section."""
        lines = ["[data]"]
        for k, v in flags.items():
            lines.append(f"{k} = {str(v).lower()}")
        return MINIMAL + "\n" + "\n".join(lines) + "\n"

    # ----- parsing -----

    def test_access_all_apps_data_parsed(self):
        m = parse_manifest_from_string(self._with_data(access_all_apps_data=True))
        assert m.access_all_apps_data is True
        assert m.access_all_apps_temp_data is False
        assert m.access_vm_data_rw is False
        assert m.access_all_data is False

    def test_access_all_apps_temp_data_parsed(self):
        m = parse_manifest_from_string(self._with_data(access_all_apps_temp_data=True))
        assert m.access_all_apps_temp_data is True

    def test_access_vm_data_rw_parsed(self):
        m = parse_manifest_from_string(self._with_data(access_vm_data_rw=True))
        assert m.access_vm_data_rw is True

    def test_vm_data_ro_and_rw_mutually_exclusive(self):
        toml = self._with_data(access_vm_data=True, access_vm_data_rw=True)
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_manifest_from_string(toml)

    def test_vm_data_ro_and_access_all_data_mutually_exclusive(self):
        # access_all_data implies RW on vm_data, so combining it with
        # an explicit RO request is contradictory.
        toml = self._with_data(access_vm_data=True, access_all_data=True)
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_manifest_from_string(toml)

    def test_vm_data_rw_and_access_all_data_is_accepted_as_redundant(self):
        # Documented in manifest_spec.md as "redundant but accepted":
        # both flags imply RW on vm_data, so there's no contradiction,
        # and the effective permissions are identical to either alone.
        m = parse_manifest_from_string(
            self._with_data(access_vm_data_rw=True, access_all_data=True)
        )
        assert m.wants_vm_data_rw is True
        assert m.wants_vm_data_ro is False

    # ----- effective-permission helpers -----

    def test_wants_own_app_data_defaults_false(self):
        m = parse_manifest_from_string(MINIMAL)
        assert m.wants_own_app_data is False
        assert m.wants_own_app_temp_data is False
        assert m.wants_all_apps_data is False
        assert m.wants_all_apps_temp_data is False
        assert m.wants_vm_data_ro is False
        assert m.wants_vm_data_rw is False

    def test_app_data_enables_own_app_data(self):
        m = parse_manifest_from_string(self._with_data(app_data=True))
        assert m.wants_own_app_data is True
        assert m.wants_all_apps_data is False

    def test_app_temp_data_enables_own_app_temp_data(self):
        m = parse_manifest_from_string(self._with_data(app_temp_data=True))
        assert m.wants_own_app_temp_data is True
        assert m.wants_own_app_data is False
        assert m.wants_all_apps_temp_data is False

    def test_sqlite_enables_own_app_data(self):
        toml = MINIMAL + '\n[data]\nsqlite = ["main"]\n'
        m = parse_manifest_from_string(toml)
        assert m.wants_own_app_data is True

    def test_access_all_apps_data_enables_both_own_and_parent(self):
        # Parent-level access implicitly covers the app's own dir — but
        # the mount layout in containers.py will shadow the scoped mount
        # with the parent.
        m = parse_manifest_from_string(self._with_data(access_all_apps_data=True))
        assert m.wants_own_app_data is True
        assert m.wants_all_apps_data is True

    def test_access_all_data_is_shorthand_for_everything(self):
        m = parse_manifest_from_string(self._with_data(access_all_data=True))
        assert m.wants_own_app_data is True
        assert m.wants_own_app_temp_data is True
        assert m.wants_all_apps_data is True
        assert m.wants_all_apps_temp_data is True
        assert m.wants_vm_data_rw is True
        # RO is suppressed because RW wins.
        assert m.wants_vm_data_ro is False

    def test_vm_data_ro_only(self):
        m = parse_manifest_from_string(self._with_data(access_vm_data=True))
        assert m.wants_vm_data_ro is True
        assert m.wants_vm_data_rw is False

    def test_vm_data_rw_suppresses_ro(self):
        m = parse_manifest_from_string(self._with_data(access_vm_data_rw=True))
        assert m.wants_vm_data_ro is False
        assert m.wants_vm_data_rw is True

    def test_independent_combination_all_apps_data_plus_vm_ro(self):
        # "I want to inspect every app's permanent data + read vm_data.
        # I do NOT want to touch temp data or modify vm_data."
        m = parse_manifest_from_string(
            self._with_data(access_all_apps_data=True, access_vm_data=True)
        )
        assert m.wants_all_apps_data is True
        assert m.wants_all_apps_temp_data is False
        assert m.wants_vm_data_ro is True
        assert m.wants_vm_data_rw is False

    def test_independent_combination_temp_data_plus_vm_rw(self):
        # Another arbitrary combination — all three categories independent.
        m = parse_manifest_from_string(
            self._with_data(access_all_apps_temp_data=True, access_vm_data_rw=True)
        )
        assert m.wants_all_apps_data is False
        assert m.wants_all_apps_temp_data is True
        assert m.wants_vm_data_rw is True
