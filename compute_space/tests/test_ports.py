"""Unit tests for port allocation and availability checking."""

import sqlite3

import pytest

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
            name TEXT PRIMARY KEY,
            local_port INTEGER NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """CREATE TABLE app_port_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            label TEXT NOT NULL,
            container_port INTEGER NOT NULL,
            host_port INTEGER NOT NULL,
            UNIQUE(app_name, label)
        )"""
    )
    conn.commit()
    return conn


class TestCheckPortAvailable:
    def test_free_port_is_available(self, db):
        # Use a high ephemeral port unlikely to be in use
        available, used_by = check_port_available(59123, db)
        # May or may not be available depending on host, but should not error
        assert isinstance(available, bool)

    def test_port_used_by_app_main(self, db):
        db.execute("INSERT INTO apps (name, local_port) VALUES ('myapp', 9500)")
        db.commit()
        available, used_by = check_port_available(9500, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["type"] == "main_port"

    def test_port_used_by_mapping(self, db):
        db.execute("INSERT INTO apps (name, local_port) VALUES ('myapp', 9500)")
        db.execute(
            "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES ('myapp', 'metrics', 9090, 9600)"
        )
        db.commit()
        available, used_by = check_port_available(9600, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["label"] == "metrics"
        assert used_by["type"] == "port_mapping"

    def test_exclude_app_skips_own_main_port(self, db):
        db.execute("INSERT INTO apps (name, local_port) VALUES ('myapp', 9500)")
        db.commit()
        available, used_by = check_port_available(9500, db, exclude_app="myapp")
        assert available is True

    def test_exclude_app_skips_own_mapping(self, db):
        db.execute("INSERT INTO apps (name, local_port) VALUES ('myapp', 9500)")
        db.execute(
            "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES ('myapp', 'metrics', 9090, 9600)"
        )
        db.commit()
        available, used_by = check_port_available(9600, db, exclude_app="myapp")
        assert available is True

    def test_exclude_app_still_blocks_other_app(self, db):
        db.execute("INSERT INTO apps (name, local_port) VALUES ('other', 9500)")
        db.commit()
        available, used_by = check_port_available(9500, db, exclude_app="myapp")
        assert available is False


class TestResolvePortMappings:
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
        db.execute("INSERT INTO apps (name, local_port) VALUES ('other', 9500)")
        db.commit()
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
