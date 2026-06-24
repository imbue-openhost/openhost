from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.pixi import ensure_pixi_version


class Migration0003PixiVersion(SystemMigration):
    version = 3

    def up(self) -> None:
        ensure_pixi_version()
