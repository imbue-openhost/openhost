import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from quart import Quart
from quart import current_app
from quart import g

from compute_space.db.migrations import migrate

# Revision id of the Alembic baseline migration. Legacy databases without an
# ``alembic_version`` table are stamped to this after running ``migrate()``.
BASELINE_REVISION = "baseline"

_ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"
_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"


def _alembic_config(db_path: str) -> AlembicConfig:
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _legacy_db_needs_cutover(db_path: str) -> bool:
    """Return True iff the DB has legacy tables but no alembic stamp."""
    conn = sqlite3.connect(db_path)
    try:
        has_alembic = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()
            is not None
        )
        if has_alembic:
            return False
        has_legacy_tables = (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            ).fetchone()
            is not None
        )
        return has_legacy_tables
    finally:
        conn.close()


def _run_legacy_cutover(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
    finally:
        conn.close()
    command.stamp(_alembic_config(db_path), BASELINE_REVISION)


def _run_alembic_upgrade(db_path: str) -> None:
    command.upgrade(_alembic_config(db_path), "head")


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
    """Bring the DB up to the current schema at boot.

    - Pre-Alembic databases (tables present, no ``alembic_version``) run the
      frozen legacy ``migrate()`` path then get stamped at the baseline
      revision.
    - Everything else (fresh DB or already-stamped DB) goes through
      ``alembic upgrade head``.
    """
    db_path = app.config["DB_PATH"]
    # Ensure WAL mode is set even on a brand-new file.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()

    if _legacy_db_needs_cutover(db_path):
        _run_legacy_cutover(db_path)
    _run_alembic_upgrade(db_path)
