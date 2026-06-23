from typing import ClassVar
from typing import Literal


class SystemMigration:
    version: ClassVar[int] = 0
    phase: ClassVar[Literal["pre_pixi_install", "post_pixi_install"]] = "post_pixi_install"

    def up(self) -> None:
        raise NotImplementedError
