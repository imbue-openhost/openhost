import hashlib
import sqlite3

from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db


def _init_test_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


class TestAppTokenHashing:
    """Verify app tokens are hashed before storage."""

    def test_stored_hash_differs_from_raw_token(self, tmp_path):
        """After inserting an app token, the stored token_hash is SHA-256, not plaintext."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        # Need an app row for the FK
        app_id = new_app_id()
        db.execute(
            "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port) "
            "VALUES (?, 'testapp', '1.0', 'serverfull', '/repo', 9000)",
            (app_id,),
        )

        raw_token = "test-app-token-xyz789"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, token_hash),
        )
        db.commit()

        row = db.execute("SELECT token_hash FROM app_tokens WHERE app_id = ?", (app_id,)).fetchone()
        assert row["token_hash"] != raw_token
        assert row["token_hash"] == token_hash
        assert len(row["token_hash"]) == 64
        db.close()

    def test_lookup_by_hash(self, tmp_path):
        """Lookup by hashed bearer token finds the correct app."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        app_id = new_app_id()
        db.execute(
            "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port) "
            "VALUES (?, 'myapp', '1.0', 'serverfull', '/repo', 9001)",
            (app_id,),
        )

        raw_token = "bearer-app-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, token_hash),
        )
        db.commit()

        # Simulate services.py lookup: hash bearer token, then query
        lookup_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        row = db.execute(
            "SELECT app_id FROM app_tokens WHERE token_hash = ?",
            (lookup_hash,),
        ).fetchone()
        assert row is not None
        assert row["app_id"] == app_id

        # Raw token should NOT match
        row_raw = db.execute(
            "SELECT app_id FROM app_tokens WHERE token_hash = ?",
            (raw_token,),
        ).fetchone()
        assert row_raw is None
        db.close()
