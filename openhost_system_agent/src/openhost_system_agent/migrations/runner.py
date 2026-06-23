from __future__ import annotations

import datetime
import fcntl
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from loguru import logger

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.migration_log import MIGRATIONS_PATH
from openhost_system_agent.migrations.migration_log import MigrationLogEntry
from openhost_system_agent.migrations.migration_log import append_entry
from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.registry import latest_registry_version
from openhost_system_agent.migrations.registry import validate_registry


def apply_system_migrations(
    migrations_path: str = MIGRATIONS_PATH,
    registry: Sequence[SystemMigration] | None = None,
    phase: Literal["pre_install", "post_install"] | None = None,
) -> list[int]:
    if registry is None:
        registry = REGISTRY
    validate_registry(registry)
    highest = latest_registry_version(registry)

    if os.geteuid() != 0:
        raise RuntimeError("System migrations must be run as root")

    Path(migrations_path).parent.mkdir(parents=True, exist_ok=True)

    lock_path = migrations_path + ".lock"
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        return _apply_under_lock(migrations_path, registry, highest, phase)


def _apply_under_lock(
    migrations_path: str,
    registry: Sequence[SystemMigration],
    highest: int,
    phase: Literal["pre_install", "post_install"] | None,
) -> list[int]:
    entries = read_log(migrations_path)
    current = current_host_version(entries)

    if current == 0:
        raise RuntimeError(
            f"No migration history found at {migrations_path}. "
            "Run ansible setup.yml to bootstrap this host before applying migrations."
        )

    if current > highest:
        raise RuntimeError(
            f"Host is at system version {current} but this code only knows up to "
            f"version {highest}. Upgrade the code before running migrations."
        )

    applied: list[int] = []
    for migration in registry:
        if migration.version <= current:
            continue
        if phase is not None and migration.phase != phase:
            continue
        source = current
        t0 = time.perf_counter()
        try:
            migration.up()
        except Exception as e:
            logger.error(f"System migration v{source} → v{migration.version} ({type(migration).__name__}) failed")
            append_entry(
                migrations_path,
                MigrationLogEntry(
                    version=migration.version,
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    success=False,
                    error=str(e),
                ),
            )
            raise
        duration = time.perf_counter() - t0
        append_entry(
            migrations_path,
            MigrationLogEntry(
                version=migration.version,
                timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                success=True,
                error=None,
            ),
        )
        logger.info(
            f"Applied system migration v{source} → v{migration.version} "
            f"({type(migration).__name__}) in {duration:.3f}s"
        )
        current = migration.version
        applied.append(migration.version)

    if not applied:
        label = f" ({phase})" if phase else ""
        logger.info(f"System is at version {current}{label} (up to date)")

    return applied
