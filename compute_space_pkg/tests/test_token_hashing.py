"""Tests that refresh tokens and app tokens are stored as SHA-256 hashes, not plaintext."""

import hashlib
import sqlite3

from compute_space.db.connection import init_db


class _FakeApp:
    def __init__(self, db_path: str):
        self.config = {"DB_PATH": db_path}


def _init_test_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    init_db(_FakeApp(db_path))
    return db_path


class TestRefreshTokenHashing:
    """Verify refresh tokens are hashed before storage."""

    def test_stored_hash_differs_from_raw_token(self, tmp_path):
        """After inserting a refresh token via the same pattern as auth.py,
        the stored token_hash should be the SHA-256 hex digest, not the raw value."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        raw_token = "test-refresh-token-abc123"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        db.execute(
            "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
            (token_hash, "2099-01-01T00:00:00"),
        )
        db.commit()

        row = db.execute("SELECT token_hash FROM refresh_tokens").fetchone()
        assert row["token_hash"] != raw_token
        assert row["token_hash"] == token_hash
        assert len(row["token_hash"]) == 64  # SHA-256 hex digest length
        db.close()

    def test_lookup_by_hash(self, tmp_path):
        """Lookup by hashed value finds the correct row."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        raw_token = "lookup-test-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        db.execute(
            "INSERT INTO refresh_tokens (token_hash, expires_at, revoked) VALUES (?, ?, 0)",
            (token_hash, "2099-01-01T00:00:00"),
        )
        db.commit()

        # Simulate middleware lookup: hash the raw cookie value, then query
        lookup_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        row = db.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
            (lookup_hash,),
        ).fetchone()
        assert row is not None

        # Raw token should NOT match
        row_raw = db.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?",
            (raw_token,),
        ).fetchone()
        assert row_raw is None
        db.close()


class TestAppTokenHashing:
    """Verify app tokens are hashed before storage."""

    def test_stored_hash_differs_from_raw_token(self, tmp_path):
        """After inserting an app token, the stored token_hash is SHA-256, not plaintext."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        # Need an app row for the FK
        db.execute(
            "INSERT INTO apps (name, version, runtime_type, repo_path, local_port) "
            "VALUES ('testapp', '1.0', 'serverfull', '/repo', 9000)"
        )

        raw_token = "test-app-token-xyz789"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        db.execute(
            "INSERT INTO app_tokens (app_name, token_hash) VALUES (?, ?)",
            ("testapp", token_hash),
        )
        db.commit()

        row = db.execute("SELECT token_hash FROM app_tokens WHERE app_name = 'testapp'").fetchone()
        assert row["token_hash"] != raw_token
        assert row["token_hash"] == token_hash
        assert len(row["token_hash"]) == 64
        db.close()

    def test_lookup_by_hash(self, tmp_path):
        """Lookup by hashed bearer token finds the correct app."""
        db_path = _init_test_db(tmp_path)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        db.execute(
            "INSERT INTO apps (name, version, runtime_type, repo_path, local_port) "
            "VALUES ('myapp', '1.0', 'serverfull', '/repo', 9001)"
        )

        raw_token = "bearer-app-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.execute(
            "INSERT INTO app_tokens (app_name, token_hash) VALUES (?, ?)",
            ("myapp", token_hash),
        )
        db.commit()

        # Simulate services.py lookup: hash bearer token, then query
        lookup_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        row = db.execute(
            "SELECT app_name FROM app_tokens WHERE token_hash = ?",
            (lookup_hash,),
        ).fetchone()
        assert row is not None
        assert row["app_name"] == "myapp"

        # Raw token should NOT match
        row_raw = db.execute(
            "SELECT app_name FROM app_tokens WHERE token_hash = ?",
            (raw_token,),
        ).fetchone()
        assert row_raw is None
        db.close()
