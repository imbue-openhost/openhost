from __future__ import annotations

from typing import ClassVar
from typing import Literal

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.pixi import ensure_pixi_version


class Migration0003PixiVersion(SystemMigration):
    version = 3
    phase: ClassVar[Literal["pre_install"]] = "pre_install"

    def up(self) -> None:
        ensure_pixi_version()
