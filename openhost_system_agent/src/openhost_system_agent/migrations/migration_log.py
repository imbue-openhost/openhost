from __future__ import annotations

import json
from pathlib import Path

import attr
from loguru import logger

MIGRATIONS_PATH = "/etc/openhost/migrations.jsonl"


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
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # If the existing file doesn't end in a newline (e.g. an external writer
    # forgot one), prepend one so we don't run two JSON objects together on
    # the same line — read_log would then skip both as malformed.
    existing = p.read_bytes() if p.exists() else b""
    if existing and not existing.endswith(b"\n"):
        line = "\n" + line
    with open(p, "a") as f:
        f.write(line)
