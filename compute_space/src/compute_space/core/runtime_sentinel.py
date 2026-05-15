from __future__ import annotations

import attr

from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.runner import current_version
from openhost_system_agent.migrations.runner import highest_registered_version
from openhost_system_agent.migrations.runner import read_log

MIGRATIONS_PATH = "/etc/openhost/migrations.jsonl"


@attr.s(auto_attribs=True, frozen=True)
class HostPrepStatus:
    ok: bool
    reason: str
    message: str


def host_prep_status(path: str = MIGRATIONS_PATH) -> HostPrepStatus:
    """Check whether the host's system migration version matches what this code expects. Never raises."""
    expected = highest_registered_version(REGISTRY)

    entries = read_log(path)
    current = current_version(entries)

    if current == 0:
        return HostPrepStatus(
            ok=False,
            reason="missing",
            message=(
                f"No migration history found at {path}. This build expects the host to have been "
                f"provisioned by `ansible-playbook ansible/setup.yml`."
            ),
        )

    if current < expected:
        return HostPrepStatus(
            ok=False,
            reason="behind",
            message=(
                f"Host is at system version {current} but this code requires version {expected}. "
                f"Run `sudo openhost_system_agent update apply` to apply pending system migrations."
            ),
        )

    if current > expected:
        return HostPrepStatus(
            ok=False,
            reason="ahead",
            message=(
                f"Host is at system version {current} but this code only knows up to version {expected}. "
                f"Upgrade the code to match the host."
            ),
        )

    return HostPrepStatus(ok=True, reason="", message="system version OK")
