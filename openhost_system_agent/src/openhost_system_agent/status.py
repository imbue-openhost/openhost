from __future__ import annotations

from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.runner import MIGRATIONS_PATH
from openhost_system_agent.migrations.runner import current_version
from openhost_system_agent.migrations.runner import highest_registered_version
from openhost_system_agent.migrations.runner import read_log


def get_migration_status(migrations_path: str = MIGRATIONS_PATH) -> dict[str, object]:
    expected = highest_registered_version(REGISTRY)
    entries = read_log(migrations_path)
    current = current_version(entries)

    if current == 0:
        return {
            "ok": False,
            "reason": "missing",
            "current_version": 0,
            "expected_version": expected,
            "message": (
                f"No migration history found at {migrations_path}. This host must be "
                f"provisioned by `ansible-playbook ansible/setup.yml`."
            ),
        }

    if current < expected:
        return {
            "ok": False,
            "reason": "behind",
            "current_version": current,
            "expected_version": expected,
            "message": (
                f"Host is at system version {current} but this code requires version {expected}. "
                f"Run `sudo openhost_system_agent update apply` to apply pending system migrations."
            ),
        }

    if current > expected:
        return {
            "ok": False,
            "reason": "ahead",
            "current_version": current,
            "expected_version": expected,
            "message": (
                f"Host is at system version {current} but this code only knows up to version {expected}. "
                f"Upgrade the code to match the host."
            ),
        }

    return {
        "ok": True,
        "reason": "",
        "current_version": current,
        "expected_version": expected,
        "message": "system version OK",
    }
