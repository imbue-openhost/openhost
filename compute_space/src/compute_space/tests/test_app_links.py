"""Unit tests for serializing/deserializing manifest [[links]] for the
``apps.links`` DB column, plus the DB round-trip through ``App.from_row``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from compute_space.core.app_id import new_app_id
from compute_space.core.apps import App
from compute_space.core.apps import _serialize_links
from compute_space.core.apps import deserialize_links
from compute_space.core.manifest import AppLink
from compute_space.db.versioned import apply_migrations


def test_serialize_round_trips():
    links = [AppLink(name="admin", path="/_openhost/admin"), AppLink(name="metrics", path="/metrics")]
    assert deserialize_links(_serialize_links(links)) == links


def test_serialize_empty():
    assert _serialize_links([]) == "[]"


def test_deserialize_none_is_empty():
    assert deserialize_links(None) == []


def test_deserialize_empty_string_is_empty():
    assert deserialize_links("") == []


def test_deserialize_malformed_json_is_empty():
    assert deserialize_links("{not json") == []


def test_deserialize_skips_entries_missing_fields():
    raw = '[{"name": "ok", "path": "/ok"}, {"name": "no-path"}, {"path": "/no-name"}, {}]'
    assert deserialize_links(raw) == [AppLink(name="ok", path="/ok")]


def test_deserialize_skips_non_string_fields():
    raw = '[{"name": 1, "path": "/x"}, {"name": "y", "path": 2}, {"name": "z", "path": "/z"}]'
    assert deserialize_links(raw) == [AppLink(name="z", path="/z")]


def _insert_app(db: sqlite3.Connection, *, port: int, links_json: str) -> str:
    app_id = new_app_id()
    db.execute(
        """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, links)
           VALUES (?, 'linky', '1.0', '/repo/linky', ?, 'running', ?)""",
        (app_id, port, links_json),
    )
    db.commit()
    return app_id


def test_from_row_loads_links_as_applink_objects(tmp_path: Path):
    db_path = str(tmp_path / "links.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        _insert_app(
            db,
            port=21001,
            links_json='[{"name": "admin", "path": "/_openhost/admin"}]',
        )
        row = db.execute("SELECT * FROM apps WHERE name = 'linky'").fetchone()
        app = App.from_row(row)
        assert app.links == [AppLink(name="admin", path="/_openhost/admin")]
    finally:
        db.close()


def test_from_row_defaults_links_to_empty(tmp_path: Path):
    """Rows created before the manifest carried links default to '[]'."""
    db_path = str(tmp_path / "links_default.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        # Omit links entirely; the column default ('[]') must apply.
        app_id = new_app_id()
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, 'nolinks', '1.0', '/repo/nolinks', 21002, 'running')""",
            (app_id,),
        )
        db.commit()
        row = db.execute("SELECT * FROM apps WHERE name = 'nolinks'").fetchone()
        app = App.from_row(row)
        assert app.links == []
    finally:
        db.close()
