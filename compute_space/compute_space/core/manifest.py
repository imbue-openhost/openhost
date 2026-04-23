import os
import tomllib
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from compute_space.core.logging import logger


@dataclass(frozen=True)
class PortMapping:
    """A structured port mapping declared in [[ports]].

    Defined as a dataclass (rather than an attrs class) so that the manifest,
    which is itself a dataclass, serializes cleanly via ``dataclasses.asdict``.
    Flask's default JSON encoder cannot serialize attrs instances, which
    previously caused /api/clone_and_get_app_info to 500 whenever a manifest
    declared any ``[[ports]]`` entries.
    """

    label: str
    container_port: int
    host_port: int = 0  # 0 = auto-assign


@dataclass
class AppManifest:
    # [app]
    name: str
    version: str
    description: str = ""
    authors: list[str] = field(default_factory=list)

    # [runtime]
    runtime_type: str = "serverfull"

    # [runtime.container]
    container_image: str = ""
    container_port: int = 0
    container_command: str | None = None
    port_mappings: list[PortMapping] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)

    # [routing]
    health_check: str | None = None
    public_paths: list[str] = field(default_factory=list)

    # [resources]
    memory_mb: int = 128
    cpu_millicores: int = 100
    gpu: bool = False

    # [data]
    sqlite_dbs: list[str] = field(default_factory=list)
    # Scoped-to-this-app access:
    app_data: bool = False
    app_temp_data: bool = False
    # Read-only access to the shared VM data directory:
    access_vm_data: bool = False
    # Broad access to all apps' data / VM-level shared state. Each of
    # these three fields can be requested independently — e.g. grant an
    # app access to every other app's permanent data without also
    # granting access to temp data or to vm_data. ``access_all_data``
    # is kept as legacy shorthand equivalent to setting all three.
    access_all_apps_data: bool = False
    access_all_apps_temp_data: bool = False
    access_vm_data_rw: bool = False
    # Read-only access to the OpenHost router's own state directory
    # (``{persistent_data_dir}/openhost/``: router.db, TLS cert + key,
    # Corefile, Caddyfile, signing keys, claim token). Intended for
    # full-instance backup/inspection tools. NOT implied by
    # ``access_all_data`` because existing manifests using that flag do
    # not expect router-state exposure; apps that want it must opt in
    # explicitly.
    access_openhost_state_ro: bool = False
    access_all_data: bool = False

    # [services]
    provides_services: list[str] = field(default_factory=list)
    requires_services: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # requires_services example: {"secrets": [{"key": "DB_URL", "reason": "...", "required": True}]}

    # [app] metadata
    hidden: bool = False

    raw_toml: str = ""

    # ------------------------------------------------------------------
    # Effective-permission helpers
    # ------------------------------------------------------------------
    #
    # ``access_all_data`` is legacy shorthand meaning "give me everything".
    # Rather than have every caller re-implement the "a or b or
    # access_all_data" expansion, these helpers return the resolved
    # permission set once so callers can consume them uniformly.

    @property
    def wants_own_app_data(self) -> bool:
        """True if the app should see its own ``/data/app_data/<app>`` dir.

        Requesting any of ``app_data``, ``sqlite`` entries, or the broad
        shorthands (``access_all_apps_data``, ``access_all_data``) all
        imply the app wants access to its own scoped permanent-data
        directory. This is independent of the app's other filesystem
        permissions — for example, an app with only ``app_temp_data``
        set has no permanent-data access but can still see its scoped
        temp directory.
        """
        return bool(
            self.app_data
            or self.sqlite_dbs
            or self.access_all_data
            or self.access_all_apps_data
        )

    @property
    def wants_own_app_temp_data(self) -> bool:
        """App's own ``/data/app_temp_data/<app>`` dir."""
        return bool(
            self.app_temp_data
            or self.access_all_data
            or self.access_all_apps_temp_data
        )

    @property
    def wants_all_apps_data(self) -> bool:
        """Parent ``/data/app_data`` dir, exposing every app's permanent data."""
        return bool(self.access_all_data or self.access_all_apps_data)

    @property
    def wants_all_apps_temp_data(self) -> bool:
        """Parent ``/data/app_temp_data`` dir, exposing every app's temp data."""
        return bool(self.access_all_data or self.access_all_apps_temp_data)

    @property
    def wants_vm_data_ro(self) -> bool:
        """Read-only access to ``/data/vm_data``."""
        return bool(self.access_vm_data and not self.wants_vm_data_rw)

    @property
    def wants_vm_data_rw(self) -> bool:
        """Read/write access to ``/data/vm_data``."""
        return bool(self.access_all_data or self.access_vm_data_rw)

    @property
    def wants_openhost_state_ro(self) -> bool:
        """Read-only access to ``{persistent_data_dir}/openhost/``.

        Intended for full-instance inspection / backup tools that need
        to read the router's SQLite DB, TLS material, and other
        control-plane state. Not implied by ``access_all_data``: apps
        must opt in explicitly via ``access_openhost_state_ro = true``.
        """
        return bool(self.access_openhost_state_ro)


def _parse_ports(ports_list: list[Any]) -> list[PortMapping]:
    """Parse and validate [[ports]] entries from manifest data."""
    seen_labels: set[str] = set()
    seen_container_ports: set[int] = set()
    seen_host_ports: set[int] = set()
    result: list[PortMapping] = []
    for entry in ports_list:
        if not isinstance(entry, dict):
            raise ValueError("Each [[ports]] entry must be a table")
        label = entry.get("label")
        if not label or not isinstance(label, str):
            raise ValueError("Each [[ports]] entry requires a string 'label'")
        if label in seen_labels:
            raise ValueError(f"Duplicate port label: '{label}'")
        seen_labels.add(label)
        cport = entry.get("container_port")
        if cport is None or not isinstance(cport, int) or cport < 0:
            raise ValueError(f"[[ports]] '{label}' requires a non-negative integer 'container_port'")
        if cport in seen_container_ports:
            raise ValueError(f"Duplicate container_port {cport} in [[ports]]")
        seen_container_ports.add(cport)
        hport = entry.get("host_port", 0)
        if not isinstance(hport, int) or hport < 0:
            raise ValueError(f"[[ports]] '{label}' host_port must be a non-negative integer")
        if hport != 0 and hport in seen_host_ports:
            raise ValueError(f"Duplicate host_port {hport} in [[ports]]")
        if hport != 0:
            seen_host_ports.add(hport)
        result.append(PortMapping(label=label, container_port=cport, host_port=hport))
    return result


def parse_manifest_from_string(raw_text: str) -> AppManifest:
    """Parse an openhost.toml manifest from its string content."""
    data = tomllib.loads(raw_text)

    app_section = data.get("app", {})
    if not app_section.get("name"):
        raise ValueError("Manifest missing required [app].name")
    if not app_section.get("version"):
        raise ValueError("Manifest missing required [app].version")

    runtime = data.get("runtime", {})
    runtime_type = runtime.get("type", "serverfull")
    if runtime_type not in ("serverless", "serverfull"):
        raise ValueError(f"Invalid runtime type: {runtime_type}")

    routing = data.get("routing", {})

    resources = data.get("resources", {})
    data_section = data.get("data", {})

    manifest = AppManifest(
        name=app_section["name"],
        version=app_section["version"],
        description=app_section.get("description", ""),
        authors=app_section.get("authors", []),
        hidden=app_section.get("hidden", False),
        runtime_type=runtime_type,
        health_check=routing.get("health_check"),
        public_paths=routing.get("public_paths", []),
        memory_mb=resources.get("memory_mb", 128),
        cpu_millicores=resources.get("cpu_millicores", 100),
        gpu=resources.get("gpu", False),
        sqlite_dbs=data_section.get("sqlite", []),
        app_data=data_section.get("app_data", False),
        app_temp_data=data_section.get("app_temp_data", False),
        access_vm_data=data_section.get("access_vm_data", False),
        access_all_apps_data=data_section.get("access_all_apps_data", False),
        access_all_apps_temp_data=data_section.get(
            "access_all_apps_temp_data", False
        ),
        access_vm_data_rw=data_section.get("access_vm_data_rw", False),
        access_openhost_state_ro=data_section.get(
            "access_openhost_state_ro", False
        ),
        access_all_data=data_section.get("access_all_data", False),
        raw_toml=raw_text,
    )

    # Reject obviously-contradictory combinations so manifests fail
    # fast at parse time instead of producing surprising mount layouts.
    # ``access_all_data`` implies RW on vm_data, so it conflicts with
    # the RO form just like ``access_vm_data_rw`` does.
    if manifest.access_vm_data and (
        manifest.access_vm_data_rw or manifest.access_all_data
    ):
        raise ValueError(
            "[data] access_vm_data (read-only) is mutually exclusive "
            "with access_vm_data_rw and access_all_data (which imply "
            "read-write access). Use one or the other."
        )

    # Parse [services] section
    services = data.get("services", {})
    manifest.provides_services = services.get("provides", [])

    # Parse per-service requirements (e.g. [services.secrets] keys = [...])
    for svc_name, svc_config in services.items():
        if svc_name == "provides":
            continue
        if isinstance(svc_config, dict) and "keys" in svc_config:
            manifest.requires_services[svc_name] = svc_config["keys"]

    container = runtime.get("container", {})
    if not container.get("image"):
        raise ValueError("[runtime.container].image is required")
    if not container.get("port"):
        raise ValueError("[runtime.container].port is required")
    manifest.container_image = container["image"]
    manifest.container_port = container["port"]
    manifest.container_command = container.get("command")
    manifest.capabilities = container.get("capabilities", [])
    manifest.devices = container.get("devices", [])

    manifest.port_mappings = _parse_ports(data.get("ports", []))

    # Deprecated: extra_ports (raw Docker -p strings)
    extra_ports = container.get("extra_ports", [])
    if extra_ports:
        logger.warning(
            "App '%s' uses deprecated 'extra_ports' in [runtime.container]. Migrate to [[ports]] tables instead.",
            manifest.name,
        )

    return manifest


def parse_manifest(repo_path: str) -> AppManifest:
    manifest_path = os.path.join(repo_path, "openhost.toml")
    if not os.path.exists(manifest_path):
        raise ValueError(f"No openhost.toml found at {manifest_path}")

    with open(manifest_path, "rb") as f:
        raw_bytes = f.read()

    return parse_manifest_from_string(raw_bytes.decode("utf-8"))
