from typing import ClassVar


class SystemMigration:
    version: ClassVar[int] = 0

    def up(self) -> None:
        raise NotImplementedError
