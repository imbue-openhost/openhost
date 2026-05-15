# System Migrations Plan (v4)

## Goal

A new `openhost_system_agent` CLI that owns all host-level operations:
updates (code + system migrations), remote URL management, and
self-update. The compute_space (quart app) calls it via `sudo` as a
subprocess instead of performing updates directly.

## Scope (this iteration)

Core framework: the system agent package, update commands, system
migration framework, and wiring the compute_space to call the agent.
No container test framework (comes later).

---

## 1. Package structure

New top-level package matching compute_space_cli's architecture:

```
openhost_system_agent/
├── pyproject.toml
└── src/openhost_system_agent/
    ├── __init__.py
    ├── main.py                  # cappa CLI entry point
    ├── update.py                # update fetch/show_diff/apply logic
    ├── git_ops.py               # git operations (moved from compute_space)
    └── migrations/
        ├── __init__.py
        ├── base.py              # SystemMigration base class
        ├── registry.py          # REGISTRY list
        ├── runner.py            # apply_system_migrations()
        └── versions/            # migration files go here
            └── __init__.py
```

## 2. CLI commands

Uses cappa + attrs, matching compute_space_cli:

```
openhost_system_agent update fetch
openhost_system_agent update show_diff
openhost_system_agent update apply
openhost_system_agent update set_remote <url>
openhost_system_agent update get_remote
```

### `update fetch`

Git fetch from remote. Returns JSON:
```json
{"ok": true, "state": "BEHIND_REMOTE"}
```

Possible states (reuses the existing `GitState` enum):
`UP_TO_DATE`, `BEHIND_REMOTE`, `AHEAD_OF_REMOTE`, `DIRTY`, `NO_REMOTE`.

### `update show_diff`

Returns JSON with a summary of what changed between HEAD and origin:
```json
{
  "ok": true,
  "commits": [
    {"sha": "abc1234", "message": "Add log rotation"},
    {"sha": "def5678", "message": "Bump runtime version"}
  ],
  "current_ref": "abc1234",
  "remote_ref": "def5678"
}
```

### `update apply`

The core update operation. Steps:
1. Validate git state (refuse if dirty or ahead).
2. `git reset --hard origin/{branch}` — check out new code.
3. `pixi install` — sync dependencies.
4. **Re-exec itself** to run system migrations with the new code:
   `openhost_system_agent _migrate` (internal command).
5. Return result JSON.

The re-exec after step 3 is important: `pixi install` makes the new
code importable, so the `_migrate` invocation runs the new migration
runner with any new migrations in the registry. Without re-exec, the
running process would use the old registry.

```json
{"ok": true, "ref": "def5678", "system_migrations_applied": [2, 3]}
```

### `_migrate` (internal command)

Not intended for direct user use. Applies pending system migrations.
Separated so it can run after `pixi install` gives us the new code.

### `update set_remote <url>`

Sets the git remote URL. Handles GitHub token injection if available.
Replaces `compute_space.web.routes.api.settings.set_remote`'s git logic.

### `update get_remote`

Returns the current remote URL and ref.

## 3. System migration framework

Lives inside the system agent package since the agent is what
executes them.

### Base class

Minimal — just `version` and `up()`:

```python
from typing import ClassVar


class SystemMigration:
    version: ClassVar[int] = 0

    def up(self) -> None:
        raise NotImplementedError
```

Migrations import `subprocess`, `pathlib`, `os`, etc. directly.
No helper methods on the base class.

### Registry

```python
from openhost_system_agent.migrations.base import SystemMigration

REGISTRY: list[SystemMigration] = [
    # Append new migrations here.
    # Versions MUST start at 2 and be contiguous.
]
```

### Runner

```python
MIGRATIONS_PATH = "/etc/openhost/migrations.jsonl"

def apply_system_migrations(
    migrations_path: str = MIGRATIONS_PATH,
    registry: list[SystemMigration] | None = None,
) -> list[int]:
    """Apply pending system migrations. Returns list of versions applied."""
```

Flow:
1. Acquire exclusive file lock on `{migrations_path}.lock`.
2. Read `migrations.jsonl`, find current version from last successful entry
   (0 if file missing or empty).
3. If version == 0: raise (must bootstrap via ansible first).
4. If current > highest: raise (code older than host).
5. For each migration with version > current:
   a. Call `migration.up()`.
   b. Append an entry to `migrations.jsonl` (success or failure).
   c. If failure, stop and raise.
6. Return list of applied versions.

Root check: verify `os.geteuid() == 0` at start.

Idempotency: the version is determined by the last successful entry.
A crash mid-migration leaves the log without a success entry for that
version, so the migration replays on next attempt.

## 4. Version tracking — migrations.jsonll

**File**: `/etc/openhost/migrations.jsonll`

A JSONL file (one JSON object per line) where each line is a single
migration attempt:

```
{"version": 1, "timestamp": "2025-03-15T14:32:00Z", "success": true, "error": null}
{"version": 2, "timestamp": "2025-04-01T09:15:22Z", "success": false, "error": "apt-get install failed: E: Unable to locate package foo"}
{"version": 2, "timestamp": "2025-04-01T09:20:05Z", "success": true, "error": null}
{"version": 3, "timestamp": "2025-04-01T09:20:06Z", "success": true, "error": null}
```

**Current version** = `version` field of the last entry where
`success == true`. If no successful entries exist, or the file is
missing, the current version is 0.

**Bootstrapping**: `ansible setup.yml` writes the initial
`migrations.jsonll` with a single entry for the highest known version
(so fresh installs have no pending migrations). This is the v1 entry
today, and gets bumped as migrations are added.

**Atomicity**: Each entry is appended as a single line. Append to a
regular file on a local filesystem is atomic for reasonable line
lengths. A crash mid-write at worst leaves a partial last line, which
the reader skips.

### Changes to runtime_sentinel.py

The runtime sentinel (`/etc/openhost/runtime`) and its
`EXPECTED_RUNTIME_VERSION` constant are replaced by `migrations.jsonl`.
`host_prep_status()` reads `migrations.jsonl` and compares the current
version against `highest_registered_version(REGISTRY)`.

The `runtime` field (checking for podman) is dropped — the
authoritative check is the live `container_runtime_available()` probe,
which the sentinel module's own docstring says is the real check.

The sentinel file at `/etc/openhost/runtime` can be removed from
ansible once the migration system is in place. During the transition,
both can coexist briefly.

## 5. How compute_space calls the agent

The settings API routes (`update_repo_state`, `set_remote`, etc.)
currently do git operations and pixi install directly. They change to
shell out to the system agent via `sudo`:

```python
async def _call_system_agent(*args: str) -> dict:
    result = await async_wrap(subprocess.run)(
        ["sudo", "openhost_system_agent", *args],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise SystemAgentError(result.stderr)
    return json.loads(result.stdout)
```

The update flow from the dashboard becomes:

```
User clicks "Check for updates"
  → POST /api/settings/check_for_updates
  → _call_system_agent("update", "fetch")
  → return state to UI

User clicks "Update"
  → POST /api/settings/update_repo_state
  → _call_system_agent("update", "apply")
  → trigger_restart()
```

### What moves out of compute_space

- `compute_space.core.updates.hard_checkout_and_validate` → replaced by
  agent's `update apply`
- `compute_space.core.updates.check_git_state` → replaced by agent's
  `update fetch` (which returns state)
- `compute_space.core.updates.run_pixi_install` → moves to agent
- `compute_space.core.git_ops` — the git helper functions. These are
  used by the agent, not compute_space. Move to the agent package (or
  keep in compute_space and have the agent import them — but the agent
  should be self-contained for clean separation).

What stays in compute_space:
- `RESTART_EXIT_CODE`, `trigger_restart()`, `initialize_shutdown_event()`,
  `wait_for_shutdown()`, `is_shutdown_pending()` — these are about the
  quart process lifecycle, not updates.
- `runtime_sentinel.py` — refactored to read `migrations.jsonl`. Imports
  `highest_registered_version` from the agent's registry.

## 6. Self-update

The agent updates the openhost repo, which includes itself. After
`git reset --hard` + `pixi install`, the agent's own code on disk is
the new version. The current process is still running the old code,
but it re-execs `_migrate` which picks up the new code.

No special self-update mechanism needed — it falls out naturally from
being part of the same repo. The one consideration: if the agent's
CLI interface changes (e.g., a new required flag), the calling code
in compute_space must be compatible. This is a normal cross-package
compatibility concern within the monorepo.

## 7. Output format

All commands return JSON to stdout. Logs go to stderr. This makes it
easy for compute_space to parse results while still getting human-
readable logs:

```python
# In the agent:
import json
from loguru import logger

logger.remove()
logger.add(sys.stderr)

def output(data: dict) -> None:
    print(json.dumps(data))
```

## 8. Registration in root pyproject.toml

```toml
[project.scripts]
openhost = "self_host_cli.main:main"
oh = "compute_space_cli.main:main"
openhost_system_agent = "openhost_system_agent.main:main"    # new

[tool.hatch.build.targets.wheel]
packages = [
    "self_host_cli/src/self_host_cli",
    "compute_space/src/compute_space",
    "compute_space_cli/src/compute_space_cli",
    "openhost_system_agent/src/openhost_system_agent",        # new
]
```

## 9. What to build (ordered)

### Phase 1: Package scaffold
1. Create `openhost_system_agent/` directory structure.
2. Write `pyproject.toml` (matching compute_space_cli's pattern).
3. Write `main.py` with cappa root command and `update` subcommand group.
4. Register in root `pyproject.toml` (scripts, hatch packages, testpaths,
   ruff isort known-first-party, mypy files).

### Phase 2: Migration framework
5. Write `migrations/base.py` — `SystemMigration` class.
6. Write `migrations/registry.py` — empty `REGISTRY`.
7. Write `migrations/runner.py` — migrations.jsonl read/write, version
   validation, `apply_system_migrations()`,
   `highest_registered_version()`.

### Phase 3: Update commands
8. Move git operations into the agent (or write fresh — the existing ones
   in `compute_space.core.git_ops` use GitPython which may or may not be
   the right choice for the agent).
9. Implement `update fetch` — git fetch + return state JSON.
10. Implement `update show_diff` — commits between HEAD and origin.
11. Implement `update apply` — git reset + pixi install + re-exec _migrate.
12. Implement `_migrate` — call `apply_system_migrations()`.
13. Implement `update set_remote` and `update get_remote`.

### Phase 4: Wire compute_space to use the agent
14. Add `_call_system_agent()` helper to compute_space.
15. Rewrite `check_for_updates` route to use the agent.
16. Rewrite `update_repo_state` route to use the agent.
17. Rewrite `set_remote` route to use the agent.
18. Refactor `runtime_sentinel.py` — read from `migrations.jsonl`,
    derive expected version from agent's registry.
19. Remove now-dead code from `compute_space.core.updates` (keep restart
    and shutdown logic).

### Phase 5: Unit tests
20. Runner tests: registry validation, migrations.jsonl read/write,
    current-version extraction, failed-entry handling,
    refuse-v0, refuse-downgrade, skip-when-current.
21. Update command tests: mock subprocess calls, verify JSON output format.
