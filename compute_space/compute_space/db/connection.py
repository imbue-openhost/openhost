import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from quart import Quart
from quart import g
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

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
    """Return True iff the DB has legacy tables but no alembic version stamp.

    Per REQ-CUTOVER-1, a DB needs cutover when the alembic version stamp is
    "absent or empty": either ``alembic_version`` table is missing entirely, or
    the table exists but has no ``version_num`` row. The latter can happen
    after ``stamp base`` or a manual wipe.
    """
    conn = sqlite3.connect(db_path)
    try:
        has_alembic_table = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone()
            is not None
        )
        if has_alembic_table:
            stamped = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone() is not None
            if stamped:
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


# ─── Async engine + session ────────────────────────────────────────────────

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def _enable_sqlite_pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
    """Enable WAL mode and foreign keys on every new SQLite connection."""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def init_engine(db_path: str) -> AsyncEngine:
    """Create (or return) the process-wide async SQLAlchemy engine.

    If an engine already exists against a different database URL — which
    only happens under tests that cycle temp DBs — the old engine is
    disposed synchronously and replaced. Real boot calls this exactly
    once per process.
    """
    global _engine, _session_maker
    target_url = f"sqlite+aiosqlite:///{db_path}"
    if _engine is not None:
        if str(_engine.url) == target_url:
            return _engine
        _engine.sync_engine.dispose()
    engine = create_async_engine(target_url, future=True)
    event.listen(engine.sync_engine, "connect", _enable_sqlite_pragmas)
    _engine = engine
    _session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("DB engine not initialized — call init_engine() first")
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    if _session_maker is None:
        raise RuntimeError("DB engine not initialized — call init_engine() first")
    return _session_maker


async def dispose_engine() -> None:
    """Tear down the async engine (tests)."""
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_maker = None


def get_session() -> AsyncSession:
    """Return the request-scoped ``AsyncSession`` (creates lazily)."""
    if "session" not in g:
        g.session = get_session_maker()()
    return g.session  # type: ignore[no-any-return]


async def close_session(exception: BaseException | None = None) -> None:
    """Close the request-scoped session, if any."""
    session = g.pop("session", None)
    if session is not None:
        await session.close()


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

    init_engine(db_path)
