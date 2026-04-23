import dataclasses
import random
import socket

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from compute_space.core.manifest import PortMapping
from compute_space.db.models import App
from compute_space.db.models import AppPortMapping


async def allocate_port(session: AsyncSession, range_start: int = 9000, range_end: int = 9999) -> int:
    used_ports: set[int] = set((await session.execute(select(App.local_port))).scalars().all())
    used_ports |= set((await session.execute(select(AppPortMapping.host_port))).scalars().all())

    for port in range(range_start, range_end + 1):
        if port in used_ports:
            continue
        if _port_is_bindable(port):
            return port

    raise RuntimeError(f"No free ports in range {range_start}-{range_end}")


async def check_port_available(
    port: int, session: AsyncSession, exclude_app: str | None = None
) -> tuple[bool, dict[str, str] | None]:
    """Check if a host port is available for use.

    Tests both TCP and UDP bind on 0.0.0.0, and checks DB for existing allocations.
    Returns (available, used_by) where used_by is a dict with structured fields.

    exclude_app: skip DB rows belonging to this app (used during reload/sync to
    avoid conflicting with the app's own existing mappings).
    """
    main_stmt = select(App.name).where(App.local_port == port)
    row = (await session.execute(main_stmt)).first()
    if row is not None and row.name != exclude_app:
        return False, {"app_name": row.name, "type": "main_port"}

    if exclude_app is not None:
        mapping_stmt = select(AppPortMapping.app_name, AppPortMapping.label).where(
            AppPortMapping.host_port == port, AppPortMapping.app_name != exclude_app
        )
    else:
        mapping_stmt = select(AppPortMapping.app_name, AppPortMapping.label).where(AppPortMapping.host_port == port)
    mapping = (await session.execute(mapping_stmt)).first()
    if mapping is not None:
        return False, {"app_name": mapping.app_name, "label": mapping.label, "type": "port_mapping"}

    if not _port_is_bindable(port):
        return False, {"type": "host_service"}

    return True, None


def _format_used_by(used_by: dict[str, str] | None) -> str:
    """Format a used_by dict into a human-readable string for error messages."""
    if used_by is None:
        return "unknown"
    if used_by["type"] == "main_port":
        return f"app '{used_by['app_name']}' (main port)"
    if used_by["type"] == "port_mapping":
        return f"app '{used_by['app_name']}' (port mapping '{used_by['label']}')"
    return "host-level service"


async def resolve_port_mappings(
    mappings: list[PortMapping],
    session: AsyncSession,
    range_start: int = 9000,
    range_end: int = 9999,
    exclude_app: str | None = None,
) -> list[PortMapping]:
    """Resolve port mappings: auto-assign host_port=0 entries, validate fixed ones.

    Returns new list of PortMapping with all host_ports resolved to actual values.
    Raises RuntimeError on conflicts or exhaustion.

    exclude_app: passed to check_port_available to ignore this app's own DB rows
    (used during reload/sync).
    """
    # Collect ports already claimed within this batch
    claimed: set[int] = set()
    resolved: list[PortMapping] = []

    # First pass: validate explicitly set ports
    for pm in mappings:
        if pm.host_port != 0:
            available, used_by = await check_port_available(pm.host_port, session, exclude_app=exclude_app)
            if not available:
                owner = _format_used_by(used_by)
                raise RuntimeError(f"Port {pm.host_port} for '{pm.label}' is already in use by {owner}")
            if pm.host_port in claimed:
                raise RuntimeError(f"Port {pm.host_port} requested by multiple mappings in this deploy")
            claimed.add(pm.host_port)
            resolved.append(pm)

    # Second pass: auto-assign for host_port=0
    for pm in mappings:
        if pm.host_port == 0:
            assigned = await _find_free_host_port(range_start, range_end, session, claimed, exclude_app=exclude_app)
            claimed.add(assigned)
            resolved.append(dataclasses.replace(pm, host_port=assigned))

    return resolved


async def _find_free_host_port(
    range_start: int,
    range_end: int,
    session: AsyncSession,
    exclude: set[int],
    exclude_app: str | None = None,
) -> int:
    """Find a free host port in the given range, excluding already-claimed ports."""
    used_ports: set[int] = set((await session.execute(select(App.local_port))).scalars().all())
    if exclude_app is not None:
        stmt = select(AppPortMapping.host_port).where(AppPortMapping.app_name != exclude_app)
        used_ports |= set((await session.execute(stmt)).scalars().all())
    else:
        used_ports |= set((await session.execute(select(AppPortMapping.host_port))).scalars().all())
    used_ports |= exclude

    candidates = list(range(range_start, range_end + 1))
    random.shuffle(candidates)
    for port in candidates:
        if port in used_ports:
            continue
        if _port_is_bindable(port):
            return port

    raise RuntimeError(f"No free ports in range {range_start}-{range_end} for auto-assignment")


def _port_is_bindable(port: int) -> bool:
    """Check if a port is bindable on 0.0.0.0 for both TCP and UDP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", port))
    except OSError:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True
