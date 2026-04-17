import sqlite3

from quart import Quart
from quart import current_app
from quart import g

from compute_space.db.migrations import _schema_path
from compute_space.db.migrations import migrate


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"], check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db  # type: ignore[no-any-return]


def close_db(exception: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app: Quart) -> None:
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        migrate(db)
        schema_path = _schema_path()
        with open(schema_path) as f:
            db.executescript(f.read())
    finally:
        db.close()
