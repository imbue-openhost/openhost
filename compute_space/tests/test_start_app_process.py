"""Tests for the lazy uid_map_base backfill performed by start_app_process.

Rows inserted before uid_map_base existed (or rows whose id fell outside
the subuid pool at migration time) keep a sentinel value of 0.  The first
start of such an app allocates and persists a proper value so podman
receives a real --uidmap argument and subsequent starts reuse the same
window.

We mock build_image and run_container because the flow they drive needs
a working podman daemon; the code under test here is pure Python + SQL.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.apps as apps_mod
from compute_space.config import DefaultConfig
from compute_space.core.containers import UID_MAP_BASE_START
from compute_space.core.containers import UID_MAP_WIDTH


def _minimal_app_dir(tmp_path: Path) -> str:
    """Create a tiny repo with an openhost.toml that parse_manifest accepts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openhost.toml").write_text(
        """
[app]
name = "notes"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
""".strip()
        + "\n"
    )
    # parse_manifest only needs openhost.toml, but run_container references
    # the Dockerfile path — we never actually build, though, so empty is fine.
    (repo / "Dockerfile").write_text("FROM scratch\n")
    return str(repo)


def _init_apps_db(db_path: str, *, repo_path: str, uid_map_base: int) -> int:
    """Create a fresh apps DB, insert one row, and return its app id."""
    db = sqlite3.connect(db_path)
    try:
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(apps_mod.__file__)),
            "db",
            "schema.sql",
        )
        with open(schema_path) as f:
            db.executescript(f.read())
        cur = db.execute(
            """INSERT INTO apps
               (name, manifest_name, version, repo_path, local_port,
                container_port, memory_mb, cpu_millicores, uid_map_base,
                status, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "notes",
                "notes",
                "0.1.0",
                repo_path,
                19100,
                8080,
                128,
                100,
                uid_map_base,
                "stopped",
                "",
            ),
        )
        row_id = cur.lastrowid
        assert row_id is not None
        db.commit()
        return row_id
    finally:
        db.close()


def test_start_app_process_backfills_uid_map_base_when_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = _minimal_app_dir(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    config = DefaultConfig(
        zone_domain="test.local",
        host="127.0.0.1",
        port=18080,
        data_root_dir=str(data_root),
        apps_dir_override=str(tmp_path / "noapps"),
        tls_enabled=False,
        start_caddy=False,
        port_range_start=19000,
        port_range_end=19099,
    )
    config.make_all_dirs()

    app_id = _init_apps_db(config.db_path, repo_path=repo_path, uid_map_base=0)

    # Mock the parts that need a live container runtime.
    monkeypatch.setattr(apps_mod, "build_image", lambda *_a, **_kw: "openhost-notes:latest")
    captured: dict[str, Any] = {}

    def fake_run_container(*args: Any, **kwargs: Any) -> str:
        captured["uid_map_base"] = kwargs.get("uid_map_base")
        return "fake-container-id"

    monkeypatch.setattr(apps_mod, "run_container", fake_run_container)
    # wait_for_ready would sit on an HTTP timeout otherwise; short-circuit.
    monkeypatch.setattr(apps_mod, "wait_for_ready", lambda *_a, **_kw: True)

    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        apps_mod.start_app_process("notes", db, config)

        # run_container should have been called with a non-zero base
        # derived from the app id.
        assert captured["uid_map_base"] == UID_MAP_BASE_START + app_id * UID_MAP_WIDTH

        # The persisted value should match, so subsequent starts reuse it.
        row = db.execute("SELECT uid_map_base, container_id FROM apps WHERE name = 'notes'").fetchone()
        assert row["uid_map_base"] == captured["uid_map_base"]
        assert row["container_id"] == "fake-container-id"
    finally:
        db.close()


def test_start_app_process_preserves_existing_uid_map_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an app already has a uid_map_base, start_app_process must not
    change it — any change would mean container-root's mapped host UID
    differs from run to run, which would orphan every file already on disk."""
    repo_path = _minimal_app_dir(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    config = DefaultConfig(
        zone_domain="test.local",
        host="127.0.0.1",
        port=18081,
        data_root_dir=str(data_root),
        apps_dir_override=str(tmp_path / "noapps"),
        tls_enabled=False,
        start_caddy=False,
        port_range_start=19100,
        port_range_end=19199,
    )
    config.make_all_dirs()

    preset_base = UID_MAP_BASE_START + 42 * UID_MAP_WIDTH
    _init_apps_db(config.db_path, repo_path=repo_path, uid_map_base=preset_base)

    monkeypatch.setattr(apps_mod, "build_image", lambda *_a, **_kw: "openhost-notes:latest")
    captured: dict[str, Any] = {}

    def fake_run_container(*args: Any, **kwargs: Any) -> str:
        captured["uid_map_base"] = kwargs.get("uid_map_base")
        return "fake-container-id"

    monkeypatch.setattr(apps_mod, "run_container", fake_run_container)
    monkeypatch.setattr(apps_mod, "wait_for_ready", lambda *_a, **_kw: True)

    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        apps_mod.start_app_process("notes", db, config)

        assert captured["uid_map_base"] == preset_base
        row = db.execute("SELECT uid_map_base FROM apps WHERE name = 'notes'").fetchone()
        assert row["uid_map_base"] == preset_base
    finally:
        db.close()
