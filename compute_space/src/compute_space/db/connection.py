import contextlib
import os
import sqlite3
import uuid
from collections.abc import Iterator

from quart import Quart
from quart import current_app
from quart import g

from compute_space.db.versioned import apply_migrations


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
    apply_migrations(app.config["DB_PATH"])


def schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")
