import os
import tomllib
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import attr

from compute_space.core.logging import logger

# Unprivileged port floor.  Must match net.ipv4.ip_unprivileged_port_start
# set by ansible/tasks/podman.yml.  Rootless podman containers cannot bind
# the host side of a -p mapping below this value, so we reject such
# manifests at parse time with a clear error.
UNPRIVILEGED_PORT_FLOOR = 80

# Capabilities that are safe to grant inside a rootless podman user
# namespace.  The kernel restricts these to the namespace; they do not
# grant any host privilege.  Anything outside this set is rejected at
# parse time — either because it requires real host privilege
# (SYS_ADMIN, SYS_MODULE, SYS_PTRACE, SYS_RAWIO, SYS_TIME, SYS_BOOT,
# MAC_ADMIN, MAC_OVERRIDE) or because it effectively requires
# CAP_SYS_ADMIN to do anything useful.
#
# Kept as a tight allowlist rather than a denylist so future kernel
# capabilities are denied by default until someone vets them.
SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Networking: needed by VPN-style apps (tailscale, wireguard).
        "NET_ADMIN",
        "NET_RAW",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        # File ownership / permissions within the user ns.
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "SETFCAP",
        # Process control within the user ns.
        "KILL",
        "SETUID",
        "SETGID",
        "SETPCAP",
        # Device node creation (needed by a few init systems); harmless
        # inside a rootless user namespace since mknod is restricted.
        "MKNOD",
        # Audit writes (rarely used but harmless).
        "AUDIT_WRITE",
        # ipc_lock for processes that mlock memory (e.g. some DBs).
        "IPC_LOCK",
        "IPC_OWNER",
        # Sync the container's filesystems before checkpoint / shutdown.
        "SYS_CHROOT",
    }
)


@attr.s(auto_attribs=True, frozen=True)
class PortMapping:
    """A structured port mapping declared in [[ports]]."""

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
    app_data: bool = False
    app_temp_data: bool = False
    access_vm_data: bool = False
    access_all_data: bool = False

    # [services]
    provides_services: list[str] = field(default_factory=list)
    requires_services: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # requires_services example: {"secrets": [{"key": "DB_URL", "reason": "...", "required": True}]}

    # [app] metadata
    hidden: bool = False

    raw_toml: str = ""


def _validate_capabilities(caps: list[Any]) -> list[str]:
    """Normalise and validate [runtime.container].capabilities.

    Accepts strings with or without the ``CAP_`` prefix (podman uses the
    un-prefixed form).  Rejects any capability not in SAFE_CAPABILITIES —
    those either require real host privilege or effectively require it to
    do anything useful, and granting them inside a rootless user namespace
    either silently no-ops (confusing) or grants more power than intended.
    """
    if not isinstance(caps, list):
        raise ValueError("[runtime.container].capabilities must be a list of strings")
    normalised: list[str] = []
    for entry in caps:
        if not isinstance(entry, str):
            raise ValueError(f"[runtime.container].capabilities must contain strings, got {type(entry).__name__}")
        name = entry.strip().upper()
        if name.startswith("CAP_"):
            name = name[len("CAP_") :]
        if name not in SAFE_CAPABILITIES:
            allowed = ", ".join(sorted(SAFE_CAPABILITIES))
            raise ValueError(
                f"[runtime.container].capabilities entry {entry!r} is not safe to grant under "
                f"rootless podman.  Allowed: {allowed}."
            )
        normalised.append(name)
    return normalised


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
        if hport != 0 and hport < UNPRIVILEGED_PORT_FLOOR:
            raise ValueError(
                f"[[ports]] '{label}' host_port {hport} is below the unprivileged port floor "
                f"({UNPRIVILEGED_PORT_FLOOR}); rootless podman cannot bind to it. "
                f"Use a port >= {UNPRIVILEGED_PORT_FLOOR} or route through the openhost proxy."
            )
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
        access_all_data=data_section.get("access_all_data", False),
        raw_toml=raw_text,
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
    manifest.capabilities = _validate_capabilities(container.get("capabilities", []))
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
