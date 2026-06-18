"""Pin pixi to 0.70.2 so it can read the version-7 pixi.lock.

Hosts provisioned before the lockfile bump run an older pixi that can't
parse the v7 lock. Self-update the host's pixi to the pinned version.
Idempotent: re-running on a host already at 0.70.2 is a no-op.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run

PIXI_VERSION = "0.70.2"
PIXI_BIN = "/home/host/.pixi/bin/pixi"


class Migration0003PixiVersion(SystemMigration):
    version = 3

    def up(self) -> None:
        run(PIXI_BIN, "self-update", "--version", PIXI_VERSION)
