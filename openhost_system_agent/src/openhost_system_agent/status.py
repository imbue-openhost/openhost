from __future__ import annotations

from openhost_system_agent.migrations.migration_log import MIGRATIONS_PATH
from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.registry import latest_registry_version
from openhost_system_agent.protocol import MigrationStatus


def get_migration_status(migrations_path: str = MIGRATIONS_PATH) -> MigrationStatus:
    expected = latest_registry_version(REGISTRY)
    entries = read_log(migrations_path)
    current = current_host_version(entries)

    if current == 0:
        return MigrationStatus(
            ok=False,
            reason="missing",
            current_host_version=0,
            expected_version=expected,
            message=(
                f"No migration history found at {migrations_path}. This host must be "
                f"provisioned by `ansible-playbook ansible/setup.yml`."
            ),
        )

    if current < expected:
        return MigrationStatus(
            ok=False,
            reason="behind",
            current_host_version=current,
            expected_version=expected,
            message=(
                f"Host is at system version {current} but this code requires version {expected}. "
                f"Run `sudo openhost_system_agent update apply` to apply pending system migrations."
            ),
        )

    if current > expected:
        return MigrationStatus(
            ok=False,
            reason="ahead",
            current_host_version=current,
            expected_version=expected,
            message=(
                f"Host is at system version {current} but this code only knows up to version {expected}. "
                f"Upgrade the code to match the host."
            ),
        )

    return MigrationStatus(
        ok=True,
        reason="",
        current_host_version=current,
        expected_version=expected,
        message="system version OK",
    )
