import os
import sqlite3
from contextlib import closing

DB_PATH = os.environ.get("OPENHOST_SQLITE_MAIN", "/data/app_data/oauth/sqlite/main.db")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db() -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if os.path.exists(schema_path):
        with closing(get_db()) as db, open(schema_path) as f:
            db.executescript(f.read())
