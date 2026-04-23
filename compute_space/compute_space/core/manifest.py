import os
import tomllib
from typing import Any

import attr
import cattrs

from compute_space.core.logging import logger


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
        capabilities=container.get("capabilities", []),
        devices=container.get("devices", []),
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
