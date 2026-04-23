"""Versioned migration framework for the compute_space SQLite DB.

See ``agent_docs/versioned_migrations/`` for the design goals and
requirements this system satisfies.
"""

from compute_space.db.versioned.base import SCHEMA_VERSION_DDL
from compute_space.db.versioned.base import Migration
from compute_space.db.versioned.base import SqlFileMigration
from compute_space.db.versioned.base import execute_sql_script
from compute_space.db.versioned.registry import REGISTRY
from compute_space.db.versioned.runner import apply_migrations
from compute_space.db.versioned.runner import highest_registered_version
from compute_space.db.versioned.runner import read_version
from compute_space.db.versioned.runner import validate_registry

__all__ = [
    "REGISTRY",
    "SCHEMA_VERSION_DDL",
    "Migration",
    "SqlFileMigration",
    "apply_migrations",
    "execute_sql_script",
    "highest_registered_version",
    "read_version",
    "validate_registry",
]
