"""Hand-maintained list of numbered migrations.

Append a new :class:`Migration` subclass to :data:`REGISTRY` each time a
new schema change is added. The runner validates that versions are
strictly increasing and contiguous starting at 2.
"""

from __future__ import annotations

from compute_space.db.versioned.base import Migration
from compute_space.db.versioned.migrations.v0002_noop import Migration0002Noop
from compute_space.db.versioned.migrations.v0003_drop_password_needs_set import Migration0003DropPasswordNeedsSet

# Numbered migrations in apply order. Versions MUST start at 2 and be
# contiguous. v0 (legacy) and v1 (baseline produced by the existing
# ``migrate()`` function) are handled out of band by the runner.
REGISTRY: list[Migration] = [
    Migration0002Noop(),
    Migration0003DropPasswordNeedsSet(),
]
