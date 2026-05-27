from __future__ import annotations

import json
from pathlib import Path

import attr
from loguru import logger

MIGRATIONS_PATH = "/etc/openhost/migrations.jsonl"
MIGRATIONS_LOCK_PATH = "/etc/openhost/migrations.jsonl.lock"


@attr.s(auto_attribs=True, frozen=True)
class MigrationLogEntry:
    version: int
    timestamp: str
    success: bool
    error: str | None


def read_log(path: str) -> list[MigrationLogEntry]:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return []
    entries: list[MigrationLogEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            entries.append(
                MigrationLogEntry(
                    version=raw["version"],
                    timestamp=raw.get("timestamp", ""),
                    success=raw["success"],
                    error=raw.get("error"),
                )
            )
        except (json.JSONDecodeError, KeyError):
            logger.warning(f"Skipping malformed migration log line: {line!r}")
            continue
    return entries


def current_host_version(entries: list[MigrationLogEntry]) -> int:
    for entry in reversed(entries):
        if entry.success:
            return entry.version
    return 0


def append_entry(path: str, entry: MigrationLogEntry) -> None:
    line = json.dumps(attr.asdict(entry), separators=(",", ":")) + "\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line)
