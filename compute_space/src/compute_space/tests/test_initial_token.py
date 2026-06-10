import hashlib
import sqlite3
from pathlib import Path

from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.initial_token import import_initial_api_token_hashes
from compute_space.db.connection import init_db


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TestImportInitialApiTokenHashes:
    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert import_initial_api_token_hashes(tmp_path / "nope", db) == 0
        db.close()

    def test_imports_token_and_deletes_file(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        raw_token = "raw-token-abc"
        token_file = tmp_path / "initial_api_token_hash"
        token_file.write_text(_hash(raw_token) + "\n")

        assert import_initial_api_token_hashes(token_file, db) == 1
        assert not token_file.exists()

        row = db.execute("SELECT name, expires_at FROM api_tokens").fetchone()
        assert row["name"] == "provisioned"
        assert row["expires_at"] == ""
        # The imported token authenticates as an owner-level API key.
        assert validate_api_token(raw_token, db) is not None
        assert validate_api_token("wrong-token", db) is None
        db.close()

    def test_custom_name_and_multiple_lines(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        token_file = tmp_path / "initial_api_token_hash"
        token_file.write_text(f"{_hash('tok-1')} ci token\n\n{_hash('tok-2')}\n")

        assert import_initial_api_token_hashes(token_file, db) == 2
        names = {r["name"] for r in db.execute("SELECT name FROM api_tokens").fetchall()}
        assert names == {"ci token", "provisioned"}
        db.close()

    def test_reimport_is_idempotent(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        token_file = tmp_path / "initial_api_token_hash"
        token_file.write_text(_hash("tok-1"))
        assert import_initial_api_token_hashes(token_file, db) == 1

        # Simulate a re-deploy rewriting the same hash file.
        token_file.write_text(_hash("tok-1"))
        assert import_initial_api_token_hashes(token_file, db) == 0
        count = db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
        assert count == 1
        db.close()

    def test_malformed_hash_is_skipped(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        token_file = tmp_path / "initial_api_token_hash"
        token_file.write_text("not-a-hash\n" + _hash("tok-ok") + "\n")

        assert import_initial_api_token_hashes(token_file, db) == 1
        assert not token_file.exists()
        count = db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
        assert count == 1
        db.close()

    def test_uppercase_hash_is_normalized(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        token_file = tmp_path / "initial_api_token_hash"
        token_file.write_text(_hash("tok-upper").upper())

        assert import_initial_api_token_hashes(token_file, db) == 1
        assert validate_api_token("tok-upper", db) is not None
        db.close()
