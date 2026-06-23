# System Migrations

System migrations modify host-level state (apt packages, systemd units,
toolchain versions, sysctls, etc.) that can't be managed through normal
code deploys. They run as root via `sudo openhost_system_agent update apply`.

## How an update works

When a host applies an update, the flow is:

```
old code: fetch → checkout new code → re-exec into new code's apply entrypoint
new code: pre_install migrations → pixi install → post_install migrations
```

The **re-exec handoff** after checkout is the critical design choice. The old
code (already running on the host) does the `git checkout`, then immediately
spawns a subprocess that imports and runs the *freshly checked-out* code.
This means the new code controls what happens from that point forward — the
old code never runs `pixi install` or migrations.

This is what makes the system resilient to toolchain changes: a migration
that upgrades pixi (marked `pre_install`) runs *before* `pixi install`, so
the lockfile format can change in the same release as the toolchain upgrade.

## Migration phases

Each migration declares a `phase`:

- **`pre_install`** — runs before `pixi install`. Use for changes that must
  happen before dependency installation (e.g., upgrading pixi so it can read
  a new lockfile format). These migrations can only import from stdlib and
  packages already installed in the env (attr, loguru, etc.).

- **`post_install`** — runs after `pixi install` (the default). Use for
  everything else: apt packages, systemd units, sysctl changes, config files.
  These can freely import any dependency in the lockfile.

## Writing a migration

1. Create `migrations/versions/v{NNNN}_{name}.py`:

```python
from openhost_system_agent.migrations.base import SystemMigration

class Migration{NNNN}{Name}(SystemMigration):
    version = {NNNN}
    # phase = "post_install"  (the default; set "pre_install" if needed)

    def up(self) -> None:
        # idempotent host-level changes here
        ...
```

2. Register it in `migrations/registry.py` (append to `REGISTRY`).

3. Add an entry to `migrations.json` at the repo root:
```json
{"tag": "v1.2.0", "migration": {NNNN}}
```
The `tag` is the most recent semver tag *before* this migration was written.
Set `null` if no tags exist yet.

4. Migrations must be **idempotent** — safe to re-run if a previous attempt
   failed partway through and is retried.

5. Versions must be **contiguous** starting at 2 (v1 is the ansible baseline).

## The stepping-stone guarantee

`migrations.json` at the repo root maps each migration to a base version tag.
This enables hosts that have been offline for a long time to catch up safely
by walking through intermediate tagged releases rather than jumping straight
to HEAD.

The algorithm:

1. Fetch the repo and read `migrations.json` from HEAD.
2. Find the first pending migration. Look up the next migration whose
   `tag` differs — call that tag T_next.
3. Checkout T_next. Re-exec the apply entrypoint from that checkout.
   It runs pre_install → pixi install → post_install for all pending
   migrations known at T_next.
4. Repeat from step 2 until caught up.
5. For the last group (no next tag boundary), checkout the latest tag
   (or HEAD if no tags exist).

### Why this works

- Migrations with base tag T were written assuming the host is at state ≥ T.
- By construction, all prior tag groups have been applied before T's group
  runs, so the host *is* at state ≥ T.
- The code is checked out from T_next (> T), where these migrations are
  guaranteed to exist (by the convention that migrations ship before the
  next tag is cut).

### The invariant to maintain

**All migrations written against tag T must be merged before the next tag
is cut.** If a migration misses its window, bump its `tag` in
`migrations.json` to the most recent tag before merging.

## What to be aware of

### Pre-install migrations have a restricted import surface

Pre-install migrations run *before* `pixi install`, so they can only use:
- Python stdlib
- Packages that were already installed in the previous env (attr, loguru,
  and anything else in the prior lockfile)

If your pre-install migration needs a new dependency, that dependency must
be added in a *prior* release so it's already installed when your migration
runs.

### The bootstrap release

The re-exec handoff and phased migration support didn't exist in the
original codebase. The release that introduces them must be installable by
the old update mechanism (old pixi, old `apply_update`). After that one
release, all future updates go through the new system.

Concretely: the first release ships with a lockfile the old fleet's pixi
can read. Once deployed, the pixi upgrade migration brings the fleet
forward, and subsequent releases can use the new lockfile format.

### No `down()` migrations

Migrations are forward-only. Host state (apt packages, systemd units,
sysctls) rarely reverses cleanly. Rely on idempotency and retry to
converge. If a migration is wrong, write a new migration that fixes it.

### Migration log

Applied migrations are tracked in `/etc/openhost/migrations.jsonl` —
one JSON line per attempt (success or failure). The host's current version
is the highest successful entry. Failed entries are logged but don't
advance the version, so retries pick up from the last success.

### Git remote URL changes

If the openhost repo moves to a new GitHub org or URL, GitHub's
[repository redirects](https://github.blog/news-insights/product-news/repository-redirects-are-here/)
handle HTTPS clones automatically — old URLs redirect to the new
location indefinitely. So for hosts using HTTPS remotes (the default),
a rename is transparent: `git fetch` follows the redirect without any
migration.

If you need to update the remote URL explicitly (e.g., switching from
HTTPS to SSH, or moving to a non-GitHub host where redirects aren't
available), use `set_remote_url` in the system agent — it's already
exposed via the CLI and the dashboard API. A system migration is *not*
the right tool for this: migrations run after fetch, so if the old URL
is unreachable the fetch fails before migrations even start.

### The `apply_after_checkout.py` stability contract

`apply_after_checkout.py` is the interface between old and new code.
After checkout, the old `_reexec_apply` runs the *new* checkout's copy
of this file. Its file path (relative to the package), its argv
interface, and its stdout JSON shape are load-bearing for every deployed
host. Changes must be backwards-compatible — do not move, rename, or
change the output format without a migration that updates old callers.
