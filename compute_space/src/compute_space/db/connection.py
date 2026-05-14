import contextlib
import contextvars
import sqlite3
import uuid
from collections.abc import Iterator

from compute_space.db.versioned import apply_migrations

_db_path: str | None = None
_db_var: contextvars.ContextVar[sqlite3.Connection | None] = contextvars.ContextVar("db", default=None)


@contextlib.contextmanager
def make_atomic_with_savepoint(db: sqlite3.Connection) -> Iterator[None]:
    """Create a savepoint and roll back to it if an exception is raised.

    Generally transactions make sqlite ops atomic, but for helper funcs that are called within a transaction,
    we can use savepoints to make them atomic without affecting the outer transaction.
    Then you don't have to wonder if the caller is using a transaction or not, to be sure that the helper func is atomic.
    """
    name = f"sp_{uuid.uuid4().hex}"
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
        db.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        db.execute(f"ROLLBACK TO SAVEPOINT {name}")
        db.execute(f"RELEASE SAVEPOINT {name}")
        raise


def init_db(db_path: str) -> None:
    """Configure the SQLite path used by ``get_db()`` and run migrations."""
    global _db_path
    _db_path = db_path
    apply_migrations(db_path)


def get_db() -> sqlite3.Connection:
    db = _db_var.get()
    if db is None:
        if _db_path is None:
            raise RuntimeError("init_db() must be called before get_db()")
        db = sqlite3.connect(_db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        _db_var.set(db)
    return db


def close_db(exception: BaseException | None = None) -> None:
    db = _db_var.get()
    if db is not None:
        db.close()
        _db_var.set(None)


def provide_db() -> sqlite3.Connection:
    """Litestar dependency: hand the per-request SQLite connection to a route.

    Returns the same connection that ``get_db()`` returns (contextvar-backed),
    so transitive ``get_db()`` calls from helpers in ``core/`` see the same
    connection within a request.  The connection is opened lazily on first
    access and closed once at the end of the request — by Litestar's
    ``after_request`` hook for routed paths, or by ``SubdomainProxyMiddleware``
    for the proxy short-circuit path.

    Per-request (not pooled) is the right shape for SQLite: connections are
    cheap, single-writer means pooling buys nothing, and request-scoping keeps
    transactions and any future savepoint usage cleanly contained.
    """
    return get_db()
