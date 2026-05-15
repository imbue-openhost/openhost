from __future__ import annotations

import datetime
import fcntl
import json
import os
import time
from pathlib import Path

from loguru import logger

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.registry import REGISTRY

MIGRATIONS_PATH = "/etc/openhost/migrations.jsonl"


def validate_registry(registry: list[SystemMigration]) -> None:
    if not registry:
        return
    versions = [m.version for m in registry]
    expected = list(range(2, 2 + len(versions)))
    if versions != expected:
        raise RuntimeError(
            f"Migration registry is not strictly increasing and contiguous starting at 2: "
            f"got {versions}, expected {expected}"
        )


def highest_registered_version(registry: list[SystemMigration]) -> int:
    if not registry:
        return 1
    return registry[-1].version


def read_log(path: str) -> list[dict[str, object]]:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return []
    entries: list[dict[str, object]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def current_version(entries: list[dict[str, object]]) -> int:
    for entry in reversed(entries):
        if entry.get("success"):
            return int(entry["version"])  # type: ignore[arg-type]
    return 0


def _append_entry(path: str, entry: dict[str, object]) -> None:
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line)


def apply_system_migrations(
    migrations_path: str = MIGRATIONS_PATH,
    registry: list[SystemMigration] | None = None,
) -> list[int]:
    if registry is None:
        registry = REGISTRY
    validate_registry(registry)
    highest = highest_registered_version(registry)

    if os.geteuid() != 0:
        raise RuntimeError("System migrations must be run as root")

    Path(migrations_path).parent.mkdir(parents=True, exist_ok=True)
    lock_path = f"{migrations_path}.lock"

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        return _apply_under_lock(migrations_path, registry, highest)


def _apply_under_lock(
    migrations_path: str,
    registry: list[SystemMigration],
    highest: int,
) -> list[int]:
    entries = read_log(migrations_path)
    current = current_version(entries)

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
        source = current
        t0 = time.perf_counter()
        try:
            migration.up()
        except Exception as e:
            logger.error(f"System migration v{source} → v{migration.version} ({type(migration).__name__}) failed")
            _append_entry(
                migrations_path,
                {
                    "version": migration.version,
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    "success": False,
                    "error": str(e),
                },
            )
            raise
        duration = time.perf_counter() - t0
        _append_entry(
            migrations_path,
            {
                "version": migration.version,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "success": True,
                "error": None,
            },
        )
        logger.info(
            f"Applied system migration v{source} → v{migration.version} "
            f"({type(migration).__name__}) in {duration:.3f}s"
        )
        current = migration.version
        applied.append(migration.version)

    if not applied:
        logger.info(f"System is at version {current} (up to date)")

    return applied


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr)
    try:
        applied = apply_system_migrations()
    except Exception as e:
        logger.error(str(e))
        sys.exit(1)
    print(json.dumps(applied))
