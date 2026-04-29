"""
Shared schema comparison helpers for migration tests.

Used by both the router and provider migration test suites.
"""


def get_schema_snapshot(db):
    """Return a normalised dict describing every table, column, and index."""
    snapshot = {"tables": {}, "indexes": {}}

    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()

    for (tbl_name,) in tables:
        cols = {}
        for row in db.execute(f"PRAGMA table_info({tbl_name})").fetchall():
            # row: (cid, name, type, notnull, dflt_value, pk)
            cols[row[1]] = {
                "type": row[2],
                "notnull": row[3],
                "default": row[4],
                "pk": row[5],
            }
        snapshot["tables"][tbl_name] = cols

    indexes = db.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    for name, tbl_name, sql in indexes:
        snapshot["indexes"][name] = {"table": tbl_name, "sql": sql}

    return snapshot


def normalise_default(val):
    """Strip outer single-quotes from default values for comparison."""
    if val is None:
        return None
    val = val.strip()
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    return val


def _normalise_index_sql(sql):
    """Normalise index DDL so semantically identical indexes compare equal.

    SQLite stores the original DDL text, so ``CREATE INDEX IF NOT EXISTS idx``
    and ``CREATE INDEX idx`` would differ textually despite being equivalent.
    """
    if sql is None:
        return None
    return sql.replace("IF NOT EXISTS ", "")


def assert_schemas_equal(fresh_snap, migrated_snap):
    """Compare two schema snapshots and produce a clear diff on failure."""
    fresh_tables = set(fresh_snap["tables"])
    migrated_tables = set(migrated_snap["tables"])
    assert fresh_tables == migrated_tables, (
        f"Table mismatch.\n"
        f"  Only in fresh: {fresh_tables - migrated_tables}\n"
        f"  Only in migrated: {migrated_tables - fresh_tables}"
    )

    for tbl in sorted(fresh_tables):
        fresh_cols = fresh_snap["tables"][tbl]
        migrated_cols = migrated_snap["tables"][tbl]

        fresh_names = set(fresh_cols)
        migrated_names = set(migrated_cols)
        assert fresh_names == migrated_names, (
            f"Column mismatch in table '{tbl}'.\n"
            f"  Only in fresh: {fresh_names - migrated_names}\n"
            f"  Only in migrated: {migrated_names - fresh_names}"
        )

        for col in sorted(fresh_names):
            fc = fresh_cols[col]
            mc = migrated_cols[col]
            assert fc["type"] == mc["type"], (
                f"Type mismatch: {tbl}.{col}: fresh={fc['type']!r}, migrated={mc['type']!r}"
            )
            assert fc["notnull"] == mc["notnull"], (
                f"NOT NULL mismatch: {tbl}.{col}: fresh={fc['notnull']}, migrated={mc['notnull']}"
            )
            assert fc["pk"] == mc["pk"], f"PK mismatch: {tbl}.{col}: fresh={fc['pk']}, migrated={mc['pk']}"
            fd = normalise_default(fc["default"])
            md = normalise_default(mc["default"])
            assert fd == md, f"Default mismatch: {tbl}.{col}: fresh={fd!r}, migrated={md!r}"

    # Compare indexes — names first, then definitions
    fresh_idx = set(fresh_snap["indexes"])
    migrated_idx = set(migrated_snap["indexes"])
    assert fresh_idx == migrated_idx, (
        f"Index mismatch.\n  Only in fresh: {fresh_idx - migrated_idx}\n  Only in migrated: {migrated_idx - fresh_idx}"
    )

    for idx_name in sorted(fresh_idx):
        fi = fresh_snap["indexes"][idx_name]
        mi = migrated_snap["indexes"][idx_name]
        assert fi["table"] == mi["table"], (
            f"Index table mismatch for '{idx_name}': fresh={fi['table']!r}, migrated={mi['table']!r}"
        )
        f_sql = _normalise_index_sql(fi["sql"])
        m_sql = _normalise_index_sql(mi["sql"])
        assert f_sql == m_sql, (
            f"Index SQL mismatch for '{idx_name}':\n  fresh:    {fi['sql']}\n  migrated: {mi['sql']}"
        )
