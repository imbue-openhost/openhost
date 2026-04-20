"""Tests for insert_and_deploy's uid_map_base allocation step.

The newly-inserted app row must have a uid_map_base set in the same
transaction as the insert, equal to compute_uid_map_base(lastrowid).
Failure to persist this would mean the app's first start falls back to
the lazy-backfill path — harmless in practice but it means the schema
invariant ("every app row has a non-zero uid_map_base after insert") is
violated, so the test pins the happy path.

We stub the background deploy thread and port helpers so this test
doesn't need podman or a Quart app context.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

import compute_space.core.apps as apps_mod
from compute_space.config import Config
from compute_space.core.apps import insert_and_deploy
from compute_space.core.containers import compute_uid_map_base
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db.connection import init_db

from .conftest import _make_test_config

_MANIFEST = """
[app]
name = "notes"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
""".lstrip()


class _FakeApp:
    def __init__(self, db_path: str) -> None:
        self.config = {"DB_PATH": db_path}


class _NoopThread:
    def start(self) -> None:
        pass


def _open_db(cfg: Config) -> sqlite3.Connection:
    init_db(_FakeApp(cfg.db_path))
    db = sqlite3.connect(cfg.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _stub_deps(monkeypatch: pytest.MonkeyPatch, local_port: int = 19005) -> None:
    """Neutralise everything insert_and_deploy calls out to that isn't
    relevant to the uid_map allocation being tested."""
    monkeypatch.setattr(threading, "Thread", lambda **kw: _NoopThread())
    monkeypatch.setattr(apps_mod, "allocate_port", lambda _start, _end: local_port)
    monkeypatch.setattr(
        apps_mod,
        "resolve_port_mappings",
        lambda mappings, _db, _start, _end: list(mappings),
    )
    monkeypatch.setattr(apps_mod.storage, "check_before_deploy", lambda _cfg: None)


def test_insert_and_deploy_sets_uid_map_base_from_formula(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, port=18080)
    db = _open_db(cfg)
    manifest: AppManifest = parse_manifest_from_string(_MANIFEST)
    _stub_deps(monkeypatch)

    repo_path = str(tmp_path / "repo")
    Path(repo_path).mkdir()
    (Path(repo_path) / "Dockerfile").write_text("FROM scratch\n")

    name = insert_and_deploy(
        manifest,
        repo_path,
        cfg,
        db,
        grant_permissions=set(),
    )
    assert name == "notes"

    row = db.execute("SELECT id, uid_map_base FROM apps WHERE name = 'notes'").fetchone()
    assert row is not None
    assert row["uid_map_base"] == compute_uid_map_base(row["id"])
    assert row["uid_map_base"] != 0


def test_insert_and_deploy_surfaces_pool_exhaustion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing compute_uid_map_base to raise simulates an id past the
    subuid pool; insert_and_deploy should propagate the ValueError so the
    /api/add_app route can translate it into a 400, and the filesystem
    directories created by provision_data ahead of the allocation must
    be cleaned up so a repeated failure doesn't leak one dir per retry.
    """
    cfg = _make_test_config(tmp_path, port=18081)
    db = _open_db(cfg)
    # Manifest with sqlite + app_temp_data so provision_data actually
    # creates a non-trivial filesystem layout we can assert was cleaned up.
    manifest: AppManifest = parse_manifest_from_string(
        _MANIFEST + '\n[data]\nsqlite = ["main"]\napp_temp_data = true\n'
    )
    _stub_deps(monkeypatch)

    def _boom(_app_id: int) -> int:
        raise ValueError("subuid pool exhausted")

    monkeypatch.setattr(apps_mod, "compute_uid_map_base", _boom)

    repo_path = str(tmp_path / "repo")
    Path(repo_path).mkdir()
    (Path(repo_path) / "Dockerfile").write_text("FROM scratch\n")

    with pytest.raises(ValueError, match="subuid pool"):
        insert_and_deploy(
            manifest,
            repo_path,
            cfg,
            db,
            grant_permissions=set(),
        )

    # provision_data eagerly created these; the failure path must
    # remove them so a retry doesn't leak anything.
    assert not os.path.exists(os.path.join(cfg.persistent_data_dir, "app_data", "notes"))
    assert not os.path.exists(os.path.join(cfg.temporary_data_dir, "app_temp_data", "notes"))
