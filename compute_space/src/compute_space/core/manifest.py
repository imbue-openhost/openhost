import os
import tomllib
from typing import Any

import attr
import cattrs

from compute_space.core.logging import logger

# Must match net.ipv4.ip_unprivileged_port_start from ansible/tasks/podman.yml.
# host_port values below this are rejected at parse time.
UNPRIVILEGED_PORT_FLOOR = 25

# Linux capabilities that can be safely granted inside a rootless podman
# user namespace.  Anything outside this set is rejected at parse time
# because it either requires real host privilege (SYS_ADMIN, SYS_MODULE,
# SYS_PTRACE, SYS_RAWIO, SYS_TIME, SYS_BOOT, MAC_ADMIN, MAC_OVERRIDE) or
# effectively requires CAP_SYS_ADMIN to do anything.  Allowlist (not
# denylist) so future kernel caps are denied by default.
SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Networking (VPN-style apps: tailscale, wireguard).
        "NET_ADMIN",
        "NET_RAW",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        # File ownership / permissions within the userns.
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "SETFCAP",
        # Process control within the userns.
        "KILL",
        "SETUID",
        "SETGID",
        "SETPCAP",
        # Device node creation (restricted by rootless anyway).
        "MKNOD",
        "AUDIT_WRITE",
        # mlock (some DBs) + chroot (some init systems).
        "IPC_LOCK",
        "IPC_OWNER",
        "SYS_CHROOT",
    }
)

# Host devices safe to pass through to rootless containers via the
# ``[runtime.container].devices`` list (mapped to ``podman --device``).
# Apps do NOT need to list ``/dev/null``, ``/dev/zero``, ``/dev/random``,
# ``/dev/urandom``, ``/dev/full``, ``/dev/tty`` or ``/dev/console`` here;
# podman (like Docker) mounts a default ``/dev`` with those character
# devices inside every container via the OCI runtime spec.  The allowlist
# below only exists to gate EXTRA host devices the app wants bound in on
# top of that baseline (serial adapters, FUSE, TUN/TAP).  Anything outside
# this set (e.g. ``/dev/mem``, ``/dev/kmem``, raw block devices, ``/dev/kvm``)
# is rejected at parse time.
SAFE_DEVICE_PATHS: frozenset[str] = frozenset(
    {
        "/dev/net/tun",
        "/dev/fuse",
        # First 8 slots of each serial/USB-TTY family; expand if needed.
        *(f"/dev/ttyS{i}" for i in range(8)),
        *(f"/dev/ttyUSB{i}" for i in range(8)),
        *(f"/dev/ttyACM{i}" for i in range(8)),
    }
)


# Capabilities only granted to apps that opt in to
# ``[runtime.security] privileged = true``.  Granting any of these
# effectively gives the app root-equivalent privilege on the host
# (inside its userns) and the operator must explicitly accept that
# at deploy time. Keep this set minimal and well-justified.
PRIVILEGED_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Required by Chromium's headless sandbox (jibri's recording
        # path), which needs to create user/PID/mount namespaces.
        "SYS_ADMIN",
    }
)


@attr.s(auto_attribs=True, frozen=True)
class PortMapping:
    """A structured port mapping declared in [[ports]]."""

    label: str
    container_port: int
    host_port: int = 0  # 0 = auto-assign


@attr.s(auto_attribs=True, frozen=True)
class ServiceProvides:
    service: str
    version: str
    endpoint: str


@attr.s(auto_attribs=True, frozen=True)
class PermissionV2Request:
    service: str
    # TODO: unsure of correct format.
    grants: list[dict[str, Any]] = attr.Factory(list)


@attr.s(auto_attribs=True, frozen=True)
class AppManifest:
    # [app]
    name: str
    version: str
    description: str = ""
    authors: list[str] = attr.Factory(list)

    # [runtime]
    runtime_type: str = "serverfull"

    # [runtime.container]
    container_image: str = ""
    container_port: int = 0
    container_command: str | None = None
    port_mappings: list[PortMapping] = attr.Factory(list)
    capabilities: list[str] = attr.Factory(list)
    devices: list[str] = attr.Factory(list)
    # `--shm-size` (in MiB).  0 = use podman's default (64 MiB).
    # Apps doing serious browser work (jibri) need ~2 GiB minimum.
    shm_mb: int = 0

    # [runtime.security]
    # When True, the app opts into capabilities the platform normally
    # rejects (PRIVILEGED_ONLY_CAPABILITIES) and the dashboard
    # surfaces a deploy-time warning.  Default-off; apps that need
    # it must declare it explicitly.
    privileged: bool = False

    # [routing]
    health_check: str | None = None
    public_paths: list[str] = attr.Factory(list)

    # [resources]
    memory_mb: int = 128
    cpu_millicores: int = 100
    gpu: bool = False

    # [data]
    sqlite_dbs: list[str] = attr.Factory(list)
    app_data: bool = False
    app_temp_data: bool = False
    access_vm_data: bool = False
    access_all_data: bool = False

    # [services]
    provides_services: list[str] = attr.Factory(list)
    requires_services: dict[str, list[dict[str, Any]]] = attr.Factory(dict)
    # requires_services example: {"secrets": [{"key": "DB_URL", "reason": "...", "required": True}]}

    # [services_v2]
    provides_services_v2: list[ServiceProvides] = attr.Factory(list)

    # [[permissions_v2]]
    permissions_v2: list[PermissionV2Request] = attr.Factory(list)

    # [app] metadata
    hidden: bool = False

    raw_toml: str = ""


def _validate_devices(devices: list[Any]) -> list[str]:
    """Normalise and validate ``[runtime.container].devices`` entries.

    Accepts the ``<host>[:<container>][:rwm]`` form and validates only
    the host path against ``SAFE_DEVICE_PATHS``.
    """
    if not isinstance(devices, list):
        raise ValueError("[runtime.container].devices must be a list of strings")
    validated: list[str] = []
    for entry in devices:
        if not isinstance(entry, str):
            raise ValueError(f"[runtime.container].devices must contain strings, got {type(entry).__name__}")
        host_path = entry.split(":", 1)[0].strip()
        if host_path not in SAFE_DEVICE_PATHS:
            allowed = ", ".join(sorted(SAFE_DEVICE_PATHS))
            raise ValueError(
                f"[runtime.container].devices entry {entry!r} is not in the allowlist.  Allowed host paths: {allowed}."
            )
        validated.append(entry)
    return validated


def _validate_capabilities(caps: list[Any], *, privileged: bool) -> list[str]:
    """Normalise and validate ``[runtime.container].capabilities``.

    Accepts ``CAP_`` prefix or bare names (podman uses bare).
    Anything in ``SAFE_CAPABILITIES`` is always allowed; anything in
    ``PRIVILEGED_ONLY_CAPABILITIES`` requires ``privileged = true``;
    anything else is rejected.
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
        if name in SAFE_CAPABILITIES:
            normalised.append(name)
            continue
        if name in PRIVILEGED_ONLY_CAPABILITIES:
            if not privileged:
                raise ValueError(
                    f"[runtime.container].capabilities entry {entry!r} requires "
                    f"[runtime.security] privileged = true (this gives the app effective "
                    f"root-on-host privilege inside its user namespace; operators must "
                    f"opt in at deploy time)."
                )
            normalised.append(name)
            continue
        allowed_safe = ", ".join(sorted(SAFE_CAPABILITIES))
        allowed_priv = ", ".join(sorted(PRIVILEGED_ONLY_CAPABILITIES))
        raise ValueError(
            f"[runtime.container].capabilities entry {entry!r} is not safe to grant under "
            f"rootless podman.  Always-allowed: {allowed_safe}.  Privileged-only (require "
            f"[runtime.security] privileged = true): {allowed_priv}."
        )
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


def _structure_list(data: list[Any], cls: type[Any], label: str) -> list[Any]:
    try:
        return [cattrs.structure(entry, cls) for entry in data]
    except (cattrs.ClassValidationError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid [[{label}]]: {exc}") from exc


def _parse_services_v2(data: dict[str, Any]) -> list[ServiceProvides]:
    entries = data.get("services_v2", {}).get("provides", [])
    return _structure_list(entries, ServiceProvides, "services_v2.provides")


def _parse_permissions_v2(data: dict[str, Any]) -> list[PermissionV2Request]:
    return _structure_list(data.get("permissions_v2", []), PermissionV2Request, "permissions_v2")


def _parse_requires_services(services: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for svc_name, svc_config in services.items():
        if svc_name == "provides":
            continue
        if isinstance(svc_config, dict) and "keys" in svc_config:
            result[svc_name] = svc_config["keys"]
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

    container = runtime.get("container", {})
    if not container.get("image"):
        raise ValueError("[runtime.container].image is required")
    if not container.get("port"):
        raise ValueError("[runtime.container].port is required")

    security = runtime.get("security", {})
    privileged = bool(security.get("privileged", False))
    if not isinstance(security.get("privileged", False), bool):
        raise ValueError("[runtime.security].privileged must be a boolean")

    shm_mb = container.get("shm_mb", 0)
    if not isinstance(shm_mb, int) or shm_mb < 0:
        raise ValueError("[runtime.container].shm_mb must be a non-negative integer")

    routing = data.get("routing", {})
    resources = data.get("resources", {})
    data_section = data.get("data", {})
    services = data.get("services", {})

    app_name = app_section["name"]

    # Deprecated: extra_ports (raw Docker -p strings)
    if container.get("extra_ports"):
        logger.warning(
            "App '%s' uses deprecated 'extra_ports' in [runtime.container]. Migrate to [[ports]] tables instead.",
            app_name,
        )

    return AppManifest(
        name=app_name,
        version=app_section["version"],
        description=app_section.get("description", ""),
        authors=app_section.get("authors", []),
        hidden=app_section.get("hidden", False),
        runtime_type=runtime_type,
        container_image=container["image"],
        container_port=container["port"],
        container_command=container.get("command"),
        port_mappings=_parse_ports(data.get("ports", [])),
        capabilities=_validate_capabilities(container.get("capabilities", []), privileged=privileged),
        devices=_validate_devices(container.get("devices", [])),
        shm_mb=shm_mb,
        privileged=privileged,
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
        provides_services=services.get("provides", []),
        requires_services=_parse_requires_services(services),
        provides_services_v2=_parse_services_v2(data),
        permissions_v2=_parse_permissions_v2(data),
        raw_toml=raw_text,
    )


def parse_manifest(repo_path: str) -> AppManifest:
    manifest_path = os.path.join(repo_path, "openhost.toml")
    if not os.path.exists(manifest_path):
        raise ValueError(f"No openhost.toml found at {manifest_path}")

    with open(manifest_path, "rb") as f:
        raw_bytes = f.read()

    return parse_manifest_from_string(raw_bytes.decode("utf-8"))
