import hashlib
import os
import re
import sqlite3


def _schema_path() -> str:
    return os.path.join(os.path.dirname(__file__), "schema.sql")


def _recreate_table(db: sqlite3.Connection, table_name: str, keep_cols: list[str]) -> None:
    """Recreate a table using the definition from schema.sql, preserving data.

    This handles cases where SQLite cannot ALTER TABLE (e.g. dropping columns
    with UNIQUE constraints, or changing NOT NULL on existing columns).
    ``keep_cols`` is the list of column names to copy from the old table;
    only columns that also exist in the new schema are actually copied.
    """
    with open(_schema_path()) as f:
        schema_sql = f.read()

    pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)} \([^;]+\);"
    m = re.search(pattern, schema_sql, re.DOTALL)
    if m is None:
        raise RuntimeError(f"Could not find CREATE TABLE {table_name} statement in schema.sql")

    tmp_name = f"{table_name}_new"
    create_sql = m.group().replace(f"CREATE TABLE IF NOT EXISTS {table_name}", f"CREATE TABLE {tmp_name}")

    # Drop any leftover temp table from a prior failed run so the CREATE
    # below doesn't fail if init_db() crashed mid-recreation previously.
    db.execute(f"DROP TABLE IF EXISTS {tmp_name}")

    # Determine which columns exist in both old and new tables
    db.execute(create_sql)
    new_col_info = {row[1]: row for row in db.execute(f"PRAGMA table_info({tmp_name})").fetchall()}
    common_cols = [c for c in keep_cols if c in new_col_info]
    cols_csv = ", ".join(common_cols)

    # Build SELECT expressions: wrap NOT NULL columns with COALESCE so that
    # NULL values in old rows get replaced by the column's default rather
    # than violating the constraint.  (SQLite only applies DEFAULT on INSERT
    # when a column is omitted, not when it's explicitly NULL.)
    select_exprs = []
    for c in common_cols:
        info = new_col_info[c]
        notnull, dflt = info[3], info[4]
        if notnull and dflt is not None:
            select_exprs.append(f"COALESCE({c}, {dflt})")
        else:
            select_exprs.append(c)
    select_csv = ", ".join(select_exprs)

    db.execute(f"INSERT INTO {tmp_name} ({cols_csv}) SELECT {select_csv} FROM {table_name}")
    db.execute(f"DROP TABLE {table_name}")
    db.execute(f"ALTER TABLE {tmp_name} RENAME TO {table_name}")
    db.commit()


def _recover_temp_tables(db: sqlite3.Connection) -> None:
    """Recover from a prior crash that left temp tables from _recreate_table.

    If a prior init_db() crashed after DROP TABLE <name> but before
    ALTER TABLE <name>_new RENAME TO <name>, the data only survives in
    the ``<name>_new`` temp table.  Detect this and rename it back so
    the subsequent migration sees a valid table.
    """
    for table_name in ("apps", "owner", "refresh_tokens", "app_tokens"):
        tmp_name = f"{table_name}_new"
        orig_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not orig_exists:
            tmp_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (tmp_name,),
            ).fetchone()
            if tmp_exists:
                db.execute(f"ALTER TABLE {tmp_name} RENAME TO {table_name}")
                db.commit()


def migrate(db: sqlite3.Connection) -> None:
    """Migrate older databases: add missing columns, drop obsolete ones, and recreate tables when constraints change."""
    _recover_temp_tables(db)
    cursor = db.execute("PRAGMA table_info(apps)")
    columns = {row[1] for row in cursor.fetchall()}
    if not columns:
        return  # Fresh DB — apps table doesn't exist; yoyo migration 0001 will create it
    if "public_paths" not in columns:
        db.execute("ALTER TABLE apps ADD COLUMN public_paths TEXT NOT NULL DEFAULT '[]'")
        db.commit()

    if "manifest_name" not in columns:
        db.execute("ALTER TABLE apps ADD COLUMN manifest_name TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE apps SET manifest_name = name")
        db.commit()

    # Re-read columns after potential ALTER TABLEs above so the drop-column
    # migration copies all current columns (including just-added ones).
    cursor = db.execute("PRAGMA table_info(apps)")
    columns = {row[1] for row in cursor.fetchall()}

    # Drop base_path and subdomain columns (no longer used).
    # base_path has a UNIQUE constraint so ALTER TABLE DROP won't work;
    # recreate the table with the correct schema instead.
    drop_cols = {"base_path", "subdomain"} & columns
    if drop_cols:
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            keep_cols = [c for c in columns if c not in drop_cols]
            _recreate_table(db, "apps", keep_cols)
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    # Re-read columns after potential table recreation
    cursor = db.execute("PRAGMA table_info(apps)")
    columns = {row[1] for row in cursor.fetchall()}

    if "repo_url" not in columns:
        db.execute("ALTER TABLE apps ADD COLUMN repo_url TEXT")
        db.commit()

    # Re-read columns after potential ALTER TABLE
    cursor = db.execute("PRAGMA table_info(apps)")
    columns = {row[1] for row in cursor.fetchall()}

    # Drop spin_pid column (serverless runtime removed) and update
    # runtime_type CHECK constraint.  Requires table recreation since
    # SQLite cannot drop columns with constraints.
    if "spin_pid" in columns:
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            keep_cols = [c for c in columns if c != "spin_pid"]
            _recreate_table(db, "apps", keep_cols)
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    # Migrate owner table: add password_needs_set, make password_hash nullable.
    # SQLite cannot ALTER a column's NOT NULL constraint, so we recreate the
    # table when the old schema had password_hash NOT NULL.
    cursor = db.execute("PRAGMA table_info(owner)")
    owner_rows = cursor.fetchall()
    owner_columns = {row[1] for row in owner_rows}

    needs_recreate = False
    if owner_columns:
        # Check if password_hash is still NOT NULL (old schema)
        for row in owner_rows:
            if row[1] == "password_hash" and row[3] == 1:  # notnull == 1
                needs_recreate = True
                break
        if "password_needs_set" not in owner_columns:
            needs_recreate = True

    if needs_recreate and owner_columns:
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            _recreate_table(db, "owner", list(owner_columns))
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    # Drop app_object_stores table (feature was never implemented)
    db.execute("DROP TABLE IF EXISTS app_object_stores")
    db.commit()

    # Create app_port_mappings table if it doesn't exist
    db.execute(
        """CREATE TABLE IF NOT EXISTS app_port_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            label TEXT NOT NULL,
            container_port INTEGER NOT NULL,
            host_port INTEGER NOT NULL,
            FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
            UNIQUE(app_name, label)
        )"""
    )
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_port_mappings_host_port ON app_port_mappings(host_port)")
    db.commit()

    # Migrate refresh_tokens: add token_hash column with hashed values, then
    # drop the plaintext token column via _recreate_table.
    rt_cols = {row[1] for row in db.execute("PRAGMA table_info(refresh_tokens)").fetchall()}
    if "token" in rt_cols and "token_hash" not in rt_cols:
        db.execute("ALTER TABLE refresh_tokens ADD COLUMN token_hash TEXT")
        rows = db.execute("SELECT id, token FROM refresh_tokens").fetchall()
        for row in rows:
            hashed = hashlib.sha256(row[1].encode()).hexdigest()
            db.execute("UPDATE refresh_tokens SET token_hash = ? WHERE id = ?", (hashed, row[0]))
        db.commit()
    # Drop the old plaintext token column if it still exists.
    rt_cols = {row[1] for row in db.execute("PRAGMA table_info(refresh_tokens)").fetchall()}
    if "token" in rt_cols:
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            _recreate_table(db, "refresh_tokens", [c for c in rt_cols if c != "token"])
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    # Migrate app_tokens: add token_hash column with hashed values, then
    # drop the plaintext token column via _recreate_table.
    at_cols = {row[1] for row in db.execute("PRAGMA table_info(app_tokens)").fetchall()}
    if "token" in at_cols and "token_hash" not in at_cols:
        db.execute("ALTER TABLE app_tokens ADD COLUMN token_hash TEXT")
        rows = db.execute("SELECT app_name, token FROM app_tokens").fetchall()
        for row in rows:
            hashed = hashlib.sha256(row[1].encode()).hexdigest()
            db.execute("UPDATE app_tokens SET token_hash = ? WHERE app_name = ?", (hashed, row[0]))
        db.commit()
    # Drop the old plaintext token column if it still exists.
    at_cols = {row[1] for row in db.execute("PRAGMA table_info(app_tokens)").fetchall()}
    if "token" in at_cols:
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            _recreate_table(db, "app_tokens", [c for c in at_cols if c != "token"])
        finally:
            db.execute("PRAGMA foreign_keys=ON")
