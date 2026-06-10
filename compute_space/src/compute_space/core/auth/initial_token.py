import re
import sqlite3
from pathlib import Path

from compute_space.core.logging import logger

INITIAL_API_TOKEN_DEFAULT_NAME = "provisioned"

_TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def import_initial_api_token_hashes(token_hash_file: Path, db: sqlite3.Connection) -> int:
    """Import pre-provisioned API token hashes from ``token_hash_file`` and delete the file.

    Provisioning tooling (e.g. vm-manager) can install an API token on an instance before its first boot by
    writing the token's SHA-256 hex digest to this file — the raw token never touches the instance.  Each
    non-empty line is ``<sha256-hex>[ <token name>]``; malformed lines are logged and skipped.  Inserts are
    idempotent (the hash column is unique), so re-running ansible against an already-provisioned instance is a
    no-op.  Imported tokens are owner-level with no expiry, but only become usable once the owner claims the
    instance (the setup-only app exposes no API routes).  Returns the number of tokens newly inserted.
    """
    try:
        content = token_hash_file.read_text(errors="replace")
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.error(f"Failed to read initial API token file {token_hash_file}: {exc}")
        return 0

    inserted = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        token_hash, _, name = line.partition(" ")
        token_hash = token_hash.lower()
        name = name.strip() or INITIAL_API_TOKEN_DEFAULT_NAME
        if not _TOKEN_HASH_RE.match(token_hash):
            logger.warning(f"Skipping malformed initial API token hash in {token_hash_file}")
            continue
        cursor = db.execute(
            "INSERT OR IGNORE INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, '')",
            (name, token_hash),
        )
        inserted += cursor.rowcount
    db.commit()

    try:
        token_hash_file.unlink()
    except OSError as exc:
        logger.error(f"Failed to delete initial API token file {token_hash_file}: {exc}")

    if inserted:
        logger.info(f"Imported {inserted} pre-provisioned API token(s)")
    return inserted
