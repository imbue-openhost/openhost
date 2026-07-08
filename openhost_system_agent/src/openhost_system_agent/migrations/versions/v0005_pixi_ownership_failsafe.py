"""Reclaim pixi ownership and add the startup failsafe to openhost.service.

Older openhost versions ran ``pixi install`` (and ``pixi self-update``) as root
during ``update apply``, leaving root-owned files under the host-owned pixi
trees. The host service's ``pixi run`` then fails with EACCES and won't start.

This migration heals such hosts and prevents recurrence:
  1. chown the pixi trees back to host now (fixes the current env).
  2. Rewrite openhost.service to include the ``ExecStartPre=+`` reclaim step,
     so any future root-owned residue is fixed on every boot before ``pixi
     run``, even on hosts that can't reach an update.

Both steps are idempotent. The rewritten unit is byte-identical to the one the
baseline migration now produces (both call ``build_openhost_service_unit``).
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file
from openhost_system_agent.migrations.versions.v0002_baseline import OPENHOST_SERVICE_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit
from openhost_system_agent.reclaim import reclaim_pixi_ownership


class Migration0005PixiOwnershipFailsafe(SystemMigration):
    version = 5

    def up(self) -> None:
        # Heal the current env up front so this migration's own downstream
        # `pixi install` (run as host by apply_after_checkout) succeeds.
        reclaim_pixi_ownership()

        # Persist the startup failsafe into the unit so future root-owned
        # residue self-heals on every boot.
        write_file(OPENHOST_SERVICE_PATH, build_openhost_service_unit(get_host_uid()), mode=0o644)
        run("systemctl", "daemon-reload")
