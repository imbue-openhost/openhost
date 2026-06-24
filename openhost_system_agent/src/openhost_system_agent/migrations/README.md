# System Migrations

System migrations modify host-level state (apt packages, systemd units, toolchain versions, sysctls, etc.) that can't
be managed through normal code deploys. They run as root via `sudo openhost_system_agent update apply`.

## How an update works

Updates are **tag-based**: hosts update to the latest semver tag, not the latest commit. When a host is behind, it
walks forward through intermediate tags one at a time:

```
for each tag after current, up to latest:
    checkout tag → run that tag's apply_after_checkout.py →
    migrations → pixi install
```

Running each step under the checked-out tag's code is the critical design choice. This means:

- Each tag's code controls its own migration + install sequence.
- Migrations run *before* `pixi install`, so a toolchain upgrade (like pixi) takes effect before deps are installed and
  the lockfile format can change in the same release.

## Writing a migration

1. Create `migrations/versions/v{NNNN}_{name}.py`:

```python
from openhost_system_agent.migrations.base import SystemMigration

class Migration{NNNN}{Name}(SystemMigration):
    version = {NNNN}

    def up(self) -> None:
        # idempotent host-level changes here
        ...
```

2. Register it in `migrations/registry.py` (append to `REGISTRY`).

3. Migrations must be **idempotent** — safe to re-run if a previous attempt failed partway through and is retried.

4. Versions must be **contiguous** starting at 2 (v1 is the ansible baseline).

## The stepping-stone guarantee

The tag walk ensures a host that has been offline for a long time can catch up safely by walking through each
intermediate tag rather than jumping straight to the latest.

### The algorithm

1. `apply_update` fetches tags, finds the next tag after the host's current position, checks it out, and launches
   `apply_after_checkout.py`.
2. `apply_after_checkout.py` runs migrations → pixi install at that tag's code.
3. It then checks if there's a next tag. If so, it checks it out and `os.execv`s into itself — replacing the process
   with the next tag's code.
4. This repeats until there are no more tags — the host is on the latest.

## What to be aware of

### Migrations run before `pixi install`, with a restricted import surface

Migrations run *before* this tag's `pixi install`, so the env still holds the *previous* tag's dependencies. A
migration may only use:
- Python stdlib and `subprocess` (the normal way to do host-level work)
- Packages already installed in the previous env (attr, loguru, and anything else in the prior lockfile)

This is rarely a constraint — migrations change host state (apt, sysctls, systemd, files) via `subprocess`, not the
project's Python deps. Note that `registry.py` imports every migration module at startup, so a migration module must
not import a new dependency at module top level either.

If a migration genuinely needs a new dependency from this tag's lockfile, run `subprocess.run([PIXI_BIN, "install"])`
at the top of its `up()` first. (Adding the dependency in a *prior* release so it's already installed is cleaner when
possible.)

### No `down()` migrations

Migrations are forward-only. Host state (apt packages, systemd units, sysctls) rarely reverses cleanly. Rely on
idempotency and retry to converge. If a migration is wrong, write a new migration that fixes it.

### Migration log

Applied migrations are tracked in `/etc/openhost/migrations.jsonl` — one JSON line per attempt (success or failure).
The host's current version is the highest successful entry. Failed entries are logged but don't advance the version, so
retries pick up from the last success.

### Git remote URL changes

If the openhost repo moves to a new GitHub org or URL, GitHub's
[repository redirects](https://github.blog/news-insights/product-news/repository-redirects-are-here/) handle HTTPS
clones automatically — old URLs redirect to the new location indefinitely. So for hosts using HTTPS remotes (the
default), a rename is transparent: `git fetch` follows the redirect without any migration.
