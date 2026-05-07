"""Unit tests for the openhost.toml manifest parser."""

import json

import attr
import pytest

from compute_space.core.manifest import SAFE_CAPABILITIES
from compute_space.core.manifest import SAFE_DEVICE_PATHS
from compute_space.core.manifest import UNPRIVILEGED_PORT_FLOOR
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
        assert manifest.app_archive is False
        assert manifest.access_vm_data is False
        assert manifest.access_all_data is False

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
        ``attr.asdict`` + ``json.dumps`` so that
        ``/api/clone_and_get_app_info`` can return them.
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
        info = attr.asdict(manifest)
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
        toml = MINIMAL + 'devices = ["/dev/net/tun"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.devices == ["/dev/net/tun"]


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


class TestServicesV2Parsing:
    """Verify [[services_v2.provides]] and [[permissions_v2]] parsing."""

    def test_single_service_provides(self):
        toml = (
            MINIMAL
            + """
[[services_v2.provides]]
service = "github.com/org/repo/services/secrets"
version = "0.1.0"
endpoint = "/_service_v2/"
"""
        )
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.provides_services_v2) == 1
        sp = manifest.provides_services_v2[0]
        assert sp.service == "github.com/org/repo/services/secrets"
        assert sp.version == "0.1.0"
        assert sp.endpoint == "/_service_v2/"

    def test_multiple_services_provides(self):
        toml = (
            MINIMAL
            + """
[[services_v2.provides]]
service = "github.com/org/repo/services/secrets"
version = "0.1.0"
endpoint = "/_service_v2/"

[[services_v2.provides]]
service = "github.com/org/repo/services/oauth"
version = "0.1.0"
endpoint = "/_oauth_v2/"
"""
        )
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.provides_services_v2) == 2
        assert manifest.provides_services_v2[0].service.endswith("/secrets")
        assert manifest.provides_services_v2[1].service.endswith("/oauth")
        assert manifest.provides_services_v2[1].endpoint == "/_oauth_v2/"

    def test_permissions_v2_parsing(self):
        toml = (
            MINIMAL
            + """
[[permissions_v2]]
service = "github.com/org/repo/services/oauth"
grants = [
    {provider = "google", scope = "https://www.googleapis.com/auth/gmail.readonly"},
    {provider = "github", scope = "repo"},
]
"""
        )
        manifest = parse_manifest_from_string(toml)
        assert len(manifest.permissions_v2) == 1
        perm = manifest.permissions_v2[0]
        assert perm.service == "github.com/org/repo/services/oauth"
        assert len(perm.grants) == 2
        assert perm.grants[0] == {"provider": "google", "scope": "https://www.googleapis.com/auth/gmail.readonly"}

    def test_permissions_v2_missing_service_raises(self):
        toml = MINIMAL + '\n[[permissions_v2]]\ngrants = [{key = "X"}]\n'
        with pytest.raises(ValueError, match="permissions_v2"):
            parse_manifest_from_string(toml)

    def test_services_v2_missing_version_raises(self):
        toml = MINIMAL + '\n[[services_v2.provides]]\nservice = "github.com/x"\nendpoint = "/"\n'
        with pytest.raises(ValueError, match="services_v2"):
            parse_manifest_from_string(toml)


class TestCapabilitiesValidation:
    """Rootless podman constraints on [runtime.container].capabilities."""

    def test_safe_cap_accepted(self):
        toml = MINIMAL + 'capabilities = ["NET_ADMIN"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.capabilities == ["NET_ADMIN"]

    @pytest.mark.parametrize("cap", sorted(SAFE_CAPABILITIES))
    def test_every_safe_cap_is_accepted(self, cap):
        """Every entry in SAFE_CAPABILITIES must actually parse.  By
        parametrising directly on the production frozenset, adding a
        new capability automatically adds a corresponding test case —
        a typo in the frozenset will fail this test with the exact
        unparseable entry as the pytest parameter id."""
        toml = MINIMAL + f'capabilities = ["{cap}"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.capabilities == [cap]

    def test_cap_prefix_is_stripped(self):
        """Manifests using the linux cap CAP_* prefix still parse."""
        toml = MINIMAL + 'capabilities = ["CAP_NET_ADMIN"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.capabilities == ["NET_ADMIN"]

    def test_unsafe_cap_rejected(self):
        """SYS_ADMIN grants real host privilege; must be rejected at parse time."""
        toml = MINIMAL + 'capabilities = ["SYS_ADMIN"]\n'
        with pytest.raises(ValueError, match="not safe"):
            parse_manifest_from_string(toml)

    def test_unknown_cap_rejected(self):
        """Unknown caps are denied by default (tight allowlist)."""
        toml = MINIMAL + 'capabilities = ["MADE_UP"]\n'
        with pytest.raises(ValueError, match="not safe"):
            parse_manifest_from_string(toml)

    def test_non_list_caps_rejected(self):
        toml = MINIMAL + 'capabilities = "NET_ADMIN"\n'
        with pytest.raises(ValueError, match="list of strings"):
            parse_manifest_from_string(toml)

    def test_non_string_cap_entry_rejected(self):
        """A list of caps that contains a non-string element must be
        rejected at parse time — otherwise a type error would surface
        from deep inside the runtime when ``.strip()`` is called on
        the offending entry."""
        toml = MINIMAL + "capabilities = [123]\n"
        with pytest.raises(ValueError, match="must contain strings"):
            parse_manifest_from_string(toml)


class TestDevicesValidation:
    """Rootless podman constraints on [runtime.container].devices."""

    def test_safe_device_accepted(self):
        toml = MINIMAL + 'devices = ["/dev/net/tun"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.devices == ["/dev/net/tun"]

    @pytest.mark.parametrize("device", sorted(SAFE_DEVICE_PATHS))
    def test_every_safe_device_is_accepted(self, device):
        """Every entry in SAFE_DEVICE_PATHS must actually parse.  By
        parametrising directly on the production frozenset, adding a
        new device automatically adds a corresponding test case, and
        a typo in the frozenset will fail this test with the exact
        unparseable path as the pytest parameter id."""
        toml = MINIMAL + f'devices = ["{device}"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.devices == [device]

    def test_device_with_rwm_spec_accepted(self):
        """podman --device accepts <host>:<container>:<perm> forms; the
        validator must parse off the host-path and validate that alone."""
        toml = MINIMAL + 'devices = ["/dev/net/tun:/dev/net/tun:rwm"]\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.devices == ["/dev/net/tun:/dev/net/tun:rwm"]

    def test_dev_mem_rejected(self):
        """/dev/mem exposes host RAM — must never reach the runtime."""
        toml = MINIMAL + 'devices = ["/dev/mem"]\n'
        with pytest.raises(ValueError, match="not in the allowlist"):
            parse_manifest_from_string(toml)

    def test_dev_kvm_rejected(self):
        toml = MINIMAL + 'devices = ["/dev/kvm"]\n'
        with pytest.raises(ValueError, match="not in the allowlist"):
            parse_manifest_from_string(toml)

    def test_arbitrary_block_device_rejected(self):
        toml = MINIMAL + 'devices = ["/dev/sda"]\n'
        with pytest.raises(ValueError, match="not in the allowlist"):
            parse_manifest_from_string(toml)

    def test_non_string_device_entry_rejected(self):
        toml = MINIMAL + "devices = [123]\n"
        with pytest.raises(ValueError, match="must contain strings"):
            parse_manifest_from_string(toml)

    def test_non_list_devices_rejected(self):
        toml = MINIMAL + 'devices = "/dev/net/tun"\n'
        with pytest.raises(ValueError, match="list of strings"):
            parse_manifest_from_string(toml)


class TestUnprivilegedPortFloor:
    """Rootless podman can't bind host_port < UNPRIVILEGED_PORT_FLOOR.

    Tests derive their boundary values from the production constant
    rather than hard-coding 80, so a change to the floor (e.g. if a
    future kernel permits lower unprivileged binds) automatically
    re-aligns the assertions.
    """

    def test_floor_is_accepted(self):
        floor = UNPRIVILEGED_PORT_FLOOR
        toml = MINIMAL + f'\n[[ports]]\nlabel = "http"\ncontainer_port = {floor}\nhost_port = {floor}\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings[0].host_port == floor

    def test_port_above_floor_accepted(self):
        toml = MINIMAL + '\n[[ports]]\nlabel = "https"\ncontainer_port = 443\nhost_port = 443\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings[0].host_port == 443

    def test_port_below_floor_rejected(self):
        below = UNPRIVILEGED_PORT_FLOOR - 1
        toml = MINIMAL + f'\n[[ports]]\nlabel = "low"\ncontainer_port = {below}\nhost_port = {below}\n'
        with pytest.raises(ValueError, match="unprivileged port floor"):
            parse_manifest_from_string(toml)

    def test_port_zero_still_allowed_for_autoassign(self):
        """host_port=0 means the router auto-assigns; the floor check must
        not clobber that sentinel."""
        toml = MINIMAL + '\n[[ports]]\nlabel = "auto"\ncontainer_port = 9000\nhost_port = 0\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.port_mappings[0].host_port == 0


class TestAppArchive:
    """Verify the [data].app_archive opt-in behaves correctly.

    The archive tier is a host-level abstraction backed by either
    local disk or a JuiceFS-on-S3 mount; the manifest opt-in is
    backing-agnostic.  These tests pin the manifest-level contract.
    """

    def test_app_archive_default_is_false(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.app_archive is False

    def test_app_archive_explicit_true_with_app_data(self):
        toml = MINIMAL + "\n[data]\napp_data = true\napp_archive = true\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.app_archive is True
        assert manifest.app_data is True

    def test_app_archive_with_sqlite(self):
        toml = MINIMAL + '\n[data]\nsqlite = ["main"]\napp_archive = true\n'
        manifest = parse_manifest_from_string(toml)
        assert manifest.app_archive is True
        assert manifest.sqlite_dbs == ["main"]

    def test_app_archive_alone(self):
        toml = MINIMAL + "\n[data]\napp_archive = true\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.app_archive is True

    def test_app_archive_with_access_all_data(self):
        toml = MINIMAL + "\n[data]\naccess_all_data = true\napp_archive = true\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.app_archive is True
        assert manifest.access_all_data is True


class TestRouterPermissions:
    """Verify the ``[permissions]`` manifest section.

    Apps may request privileged grants on the router itself (deploy
    other apps, etc.).  The manifest only carries the *request*; whether
    a grant is actually applied is the install-time consent flow's job.
    These tests pin the parsing contract.
    """

    def test_deploy_apps_default_is_false(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.deploy_apps_permission is False

    def test_deploy_apps_explicit_true(self):
        toml = MINIMAL + "\n[permissions]\ndeploy_apps = true\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.deploy_apps_permission is True

    def test_deploy_apps_explicit_false(self):
        toml = MINIMAL + "\n[permissions]\ndeploy_apps = false\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.deploy_apps_permission is False

    def test_deploy_apps_non_bool_rejected(self):
        # Catches the common typo ``deploy_apps = "true"`` (TOML strings
        # would otherwise parse without complaint and end up being
        # treated as truthy in Python — fail closed instead).
        toml = MINIMAL + '\n[permissions]\ndeploy_apps = "true"\n'
        with pytest.raises(ValueError, match=r"\[permissions\]\.deploy_apps must be a boolean"):
            parse_manifest_from_string(toml)

    def test_permissions_section_absent_no_grants(self):
        # No [permissions] table at all should be equivalent to all
        # router permissions defaulting to False.
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.deploy_apps_permission is False

    def test_unrelated_keys_in_permissions_section_are_ignored(self):
        # Forward-compat: unknown keys should not crash; the manifest is
        # the source of truth for *requests*, the server's
        # KNOWN_ROUTER_PERMISSIONS set is the source of truth for what's
        # actually grantable.
        toml = MINIMAL + "\n[permissions]\ndeploy_apps = true\nfuture_thing = true\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.deploy_apps_permission is True


class TestShmMb:
    """[runtime.container].shm_mb."""

    def test_default_zero(self):
        manifest = parse_manifest_from_string(MINIMAL)
        assert manifest.shm_mb == 0

    def test_shm_mb_accepted(self):
        toml = MINIMAL + "shm_mb = 2048\n"
        manifest = parse_manifest_from_string(toml)
        assert manifest.shm_mb == 2048

    def test_shm_mb_negative_rejected(self):
        toml = MINIMAL + "shm_mb = -1\n"
        with pytest.raises(ValueError, match="shm_mb"):
            parse_manifest_from_string(toml)

    def test_shm_mb_non_int_rejected(self):
        toml = MINIMAL + 'shm_mb = "big"\n'
        with pytest.raises(ValueError, match="shm_mb"):
            parse_manifest_from_string(toml)
