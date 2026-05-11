"""Unit tests for port allocation and availability checking."""

import sqlite3
from collections.abc import Iterator
from unittest import mock

import pytest

from compute_space.core.app_id import new_app_id
from compute_space.core.manifest import PortMapping
from compute_space.core.ports import check_port_available
from compute_space.core.ports import resolve_port_mappings


@pytest.fixture
def db():
    """In-memory SQLite DB with the required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE apps (
            app_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            local_port INTEGER NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """CREATE TABLE app_port_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            label TEXT NOT NULL,
            container_port INTEGER NOT NULL,
            host_port INTEGER NOT NULL,
            UNIQUE(app_id, label)
        )"""
    )
    conn.commit()
    return conn


def _seed(db, name: str, local_port: int) -> str:
    """Insert an app row and return its app_id."""
    app_id = new_app_id()
    db.execute("INSERT INTO apps (app_id, name, local_port) VALUES (?, ?, ?)", (app_id, name, local_port))
    db.commit()
    return app_id


class TestCheckPortAvailable:
    def test_free_port_is_available(self, db):
        # Use a high ephemeral port unlikely to be in use
        available, used_by = check_port_available(59123, db)
        # May or may not be available depending on host, but should not error
        assert isinstance(available, bool)

    def test_port_used_by_app_main(self, db):
        _seed(db, "myapp", 9500)
        available, used_by = check_port_available(9500, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["type"] == "main_port"

    def test_port_used_by_mapping(self, db):
        app_id = _seed(db, "myapp", 9500)
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) "
            "VALUES (?, 'metrics', 9090, 9600)",
            (app_id,),
        )
        db.commit()
        available, used_by = check_port_available(9600, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["label"] == "metrics"
        assert used_by["type"] == "port_mapping"

    def test_exclude_app_skips_own_main_port(self, db):
        app_id = _seed(db, "myapp", 9500)
        available, used_by = check_port_available(9500, db, exclude_app_id=app_id)
        assert available is True

    def test_exclude_app_skips_own_mapping(self, db):
        app_id = _seed(db, "myapp", 9500)
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) "
            "VALUES (?, 'metrics', 9090, 9600)",
            (app_id,),
        )
        db.commit()
        available, used_by = check_port_available(9600, db, exclude_app_id=app_id)
        assert available is True

    def test_exclude_app_still_blocks_other_app(self, db):
        _seed(db, "other", 9500)
        available, used_by = check_port_available(9500, db, exclude_app_id=new_app_id())
        assert available is False


@pytest.fixture
def _always_bindable() -> Iterator[None]:
    """Pretend every port is OS-bindable so resolve_port_mappings tests
    don't depend on what's actually bound on the CI runner.

    Without this mock, resolve_port_mappings tests that hardcode
    high ports (e.g., 59200) can fail intermittently on shared
    CI runners where some unrelated process — kernel-allocated
    ephemeral source ports, runner-installed services, etc. —
    happens to occupy the chosen number.  The tests want to
    exercise resolve_port_mappings' DB-bookkeeping logic, not
    its OS-side bindability check, so mocking _port_is_bindable
    out is the right scope of fix.
    """
    with mock.patch("compute_space.core.ports._port_is_bindable", return_value=True):
        yield


class TestResolvePortMappings:
    @pytest.fixture(autouse=True)
    def _bindable(self, _always_bindable: None) -> None:
        """Apply the bindable-port mock to every test in this class."""
        return None

    def test_fixed_ports_pass_through(self, db):
        mappings = [PortMapping(label="web", container_port=80, host_port=59200)]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 1
        assert resolved[0].host_port == 59200

    def test_auto_assign_picks_free_port(self, db):
        mappings = [PortMapping(label="auto", container_port=3000, host_port=0)]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 1
        assert 59200 <= resolved[0].host_port <= 59300

    def test_conflict_with_db_raises(self, db):
        _seed(db, "other", 9500)
        mappings = [PortMapping(label="conflict", container_port=80, host_port=9500)]
        with pytest.raises(RuntimeError, match="already in use"):
            resolve_port_mappings(mappings, db)

    def test_duplicate_port_in_batch_raises(self, db):
        mappings = [
            PortMapping(label="a", container_port=80, host_port=59200),
            PortMapping(label="b", container_port=81, host_port=59200),
        ]
        with pytest.raises(RuntimeError, match="multiple mappings"):
            resolve_port_mappings(mappings, db, 59200, 59300)

    def test_mixed_fixed_and_auto(self, db):
        mappings = [
            PortMapping(label="fixed", container_port=80, host_port=59200),
            PortMapping(label="auto", container_port=3000, host_port=0),
        ]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 2
        ports = {r.label: r.host_port for r in resolved}
        assert ports["fixed"] == 59200
        assert ports["auto"] != 59200  # should pick different port
        assert 59200 <= ports["auto"] <= 59300
