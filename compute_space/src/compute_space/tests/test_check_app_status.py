"""Tests for ``check_app_status`` startup recovery.

The key regression: an app left in 'starting' after an interrupted boot-time
restart sweep must still be recovered. Earlier the sweep only looked at
'running' apps, so anything stranded in 'starting' stayed down forever.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import compute_space.core.startup as startup
from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db

from .conftest import _make_test_config


def _seed_app(
    cfg: Any,
    *,
    name: str,
    status: str,
    port: int,
    container_id: str | None,
    repo_path: str,
) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id)
               VALUES (?, ?, '1.0', ?, ?, ?, ?)""",
            (app_id, name, repo_path, port, status, container_id),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _status(cfg: Any, app_id: str) -> str:
    db = sqlite3.connect(cfg.db_path)
    try:
        status: str = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
        return status
    finally:
        db.close()


def _capture_restart_sweep(monkeypatch: Any) -> tuple[list[str], threading.Event]:
    """Replace the background restart sweep with a recorder."""
    restarted: list[str] = []
    done = threading.Event()

    def fake_sequential(app_ids: list[str], config: Any) -> None:
        restarted.extend(app_ids)
        done.set()

    monkeypatch.setattr(startup, "_restart_apps_sequential", fake_sequential)
    return restarted, done


def test_starting_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="stuck", status="starting", port=20210, container_id="deadbeef", repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for the stranded 'starting' app"
    assert app_id in restarted


def test_building_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20400)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(
        cfg, name="mid-build", status="building", port=20410, container_id="deadbeef", repo_path=str(repo)
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert app_id in restarted


def test_starting_app_with_live_container_is_healed_to_running(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20300)
    init_db(cfg.db_path)
    app_id = _seed_app(
        cfg, name="live", status="starting", port=20310, container_id="livecontainer", repo_path="/nonexistent"
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert _status(cfg, app_id) == "running"


def test_running_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20500)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="crashed", status="running", port=20510, container_id="deadbeef", repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert app_id in restarted


def test_starting_app_with_no_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20700)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="nocontainer", status="starting", port=20710, container_id=None, repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for 'starting' app with no container"
    assert app_id in restarted
    assert _status(cfg, app_id) == "starting"


def test_building_app_with_no_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20800)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(
        cfg, name="nocontainer-build", status="building", port=20810, container_id=None, repo_path=str(repo)
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for 'building' app with no container"
    assert app_id in restarted
    assert _status(cfg, app_id) == "starting"


def test_running_app_with_live_container_is_left_alone(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20600)
    init_db(cfg.db_path)
    app_id = _seed_app(
        cfg, name="healthy", status="running", port=20610, container_id="livecontainer", repo_path="/nonexistent"
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert restarted == []
    assert _status(cfg, app_id) == "running"
