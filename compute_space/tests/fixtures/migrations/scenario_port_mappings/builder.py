"""
Seed data focused on the ``app_port_mappings`` table.

Two apps (``app_a``, ``app_b``) each with three port mappings at distinct
``container_port`` / ``host_port`` values and distinct labels.  Enough
owner + apps rows to satisfy the foreign keys on ``apps.name``.
"""

from __future__ import annotations

import sqlite3

_FIXED_CREATED_AT = "2025-01-01T00:00:00"


def seed_at(conn: sqlite3.Connection, migration_id: str) -> None:
    if migration_id == "0001_initial":
        _seed_baseline(conn)


def _seed_baseline(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO owner (id, username, password_hash, password_needs_set, created_at) VALUES (?, ?, ?, ?, ?)",
        (1, "admin", None, 1, _FIXED_CREATED_AT),
    )

    # Provide the minimum ``apps`` columns FK targets need and pin deterministic
    # defaults for every column that has one so snapshots are stable even when
    # a column's server-side default later changes.
    conn.executemany(
        "INSERT INTO apps (id, name, manifest_name, version, repo_path, local_port, "
        "runtime_type, status, memory_mb, cpu_millicores, gpu, public_paths, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "app_a",
                "app_a",
                "1.0",
                "/repos/app_a",
                9001,
                "serverfull",
                "stopped",
                128,
                100,
                0,
                "[]",
                _FIXED_CREATED_AT,
                _FIXED_CREATED_AT,
            ),
            (
                2,
                "app_b",
                "app_b",
                "1.0",
                "/repos/app_b",
                9002,
                "serverfull",
                "stopped",
                128,
                100,
                0,
                "[]",
                _FIXED_CREATED_AT,
                _FIXED_CREATED_AT,
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO app_port_mappings (id, app_name, label, container_port, host_port) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "app_a", "http", 8080, 20001),
            (2, "app_a", "metrics", 9100, 20002),
            (3, "app_a", "admin", 8443, 20003),
            (4, "app_b", "http", 8080, 20101),
            (5, "app_b", "grpc", 50051, 20102),
            (6, "app_b", "debug", 5858, 20103),
        ],
    )
