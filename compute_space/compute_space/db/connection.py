import os
import sqlite3

from quart import Quart
from quart import current_app
from quart import g
from yoyo import get_backend
from yoyo import read_migrations

from compute_space.db.legacy_migrate import migrate

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


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


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _classify_db_state(db: sqlite3.Connection) -> str:
    """Return one of 'managed', 'legacy', 'fresh'.

    managed: _yoyo_migration table present → yoyo owns schema state
    legacy:  apps table present, no yoyo table → pre-yoyo database
    fresh:   neither table present → empty file
    """
    if _table_exists(db, "_yoyo_migration"):
        return "managed"
    # `apps_new` may be present instead of `apps` when a prior run crashed
    # mid-way through _recreate_table; legacy migrate() knows how to recover.
    if _table_exists(db, "apps") or _table_exists(db, "apps_new"):
        return "legacy"
    return "fresh"


def init_db(app: Quart) -> None:
    """Bring the database to the latest schema.

    Dispatches on three possible starting states:
      - fresh: apply all yoyo migrations (0001 contains the full baseline)
      - legacy: run the frozen legacy migrate() once to cover the
        imperative ALTER/recreate steps, then apply all yoyo migrations.
        0001 is composed entirely of CREATE ... IF NOT EXISTS statements,
        so re-applying it on a legacy DB is a no-op for tables migrate()
        already built and fills in any tables it doesn't touch.
      - managed: apply any pending yoyo migrations only.

    The legacy migrate() MUST NOT run on a managed database.
    """
    db_path = app.config["DB_PATH"]

    db = sqlite3.connect(db_path)
    try:
        state = _classify_db_state(db)
        if state == "legacy":
            migrate(db)
    finally:
        db.close()

    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(MIGRATIONS_DIR)
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))
