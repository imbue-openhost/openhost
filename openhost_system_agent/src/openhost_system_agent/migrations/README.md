# System Migrations

System migrations modify host-level state (apt packages, systemd units,
toolchain versions, sysctls, etc.) that can't be managed through normal
code deploys. They run as root via `sudo openhost_system_agent update apply`.

## How an update works

Updates are **tag-based**: hosts update to the latest semver tag, not
the latest commit. When a host is behind, it walks forward through
intermediate tags one at a time:

```
for each tag after current, up to latest:
    checkout tag → re-exec apply_after_checkout.py →
    pre_pixi_install migrations → pixi install → post_pixi_install migrations
```

The **re-exec** at each tag is the critical design choice. The code that
runs at each step is the code *from that tag*, not the code that started
the update. This means:

- Each tag's code controls its own migration + install sequence.
- Toolchain upgrades (like pixi) marked `pre_pixi_install` run before
  `pixi install`, so the lockfile format can change in the same release.

## Migration phases

Each migration declares a `phase`:

- **`pre_pixi_install`** — runs before `pixi install`. Use for changes
  that must happen before dependency installation (e.g., upgrading pixi
  so it can read a new lockfile format). These migrations can only import
  from stdlib and packages already installed in the env (attr, loguru).

- **`post_pixi_install`** — runs after `pixi install` (the default). Use
  for everything else: apt packages, systemd units, sysctl changes,
  config files. These can freely import any dependency in the lockfile.

## Writing a migration

1. Create `migrations/versions/v{NNNN}_{name}.py`:

```python
from openhost_system_agent.migrations.base import SystemMigration

class Migration{NNNN}{Name}(SystemMigration):
    version = {NNNN}
    # phase = "post_pixi_install"  (the default; set "pre_pixi_install" if needed)

    def up(self) -> None:
        # idempotent host-level changes here
        ...
```

2. Register it in `migrations/registry.py` (append to `REGISTRY`).

3. Migrations must be **idempotent** — safe to re-run if a previous
   attempt failed partway through and is retried.

4. Versions must be **contiguous** starting at 2 (v1 is the ansible
   baseline).

## The stepping-stone guarantee

The tag walk ensures a host that has been offline for a long time can
catch up safely by walking through each intermediate tag rather than
jumping straight to the latest.

### The algorithm

1. `apply_update` fetches tags, finds the next tag after the host's
   current position, checks it out, and re-execs `apply_after_checkout.py`.
2. `apply_after_checkout.py` runs pre_pixi_install migrations → pixi
   install → post_pixi_install migrations at that tag's code.
3. It then checks if there's a next tag. If so, it checks it out and
   re-execs itself (a fresh process using the next tag's code).
4. This repeats until there are no more tags — the host is on the latest.

### Why this works

- At each tag, the re-exec'd process imports the code *from that tag*.
- Each step can only advance forward (tags are sorted by semver).
- Migrations are in the registry at the tag where they were added, so
  they run at the right code version by construction.

### The invariant to maintain

**Migrations must be merged before the next tag is cut.** The tag walk
runs each tag's registry, so a migration only runs if it exists in the
registry at or before the tag being applied.

## What to be aware of

### Pre-pixi-install migrations have a restricted import surface

Pre-pixi-install migrations run *before* `pixi install`, so they can
only use:
- Python stdlib
- Packages already installed in the previous env (attr, loguru, and
  anything else in the prior lockfile)

If your pre-pixi-install migration needs a new dependency, that
dependency must be added in a *prior* release so it's already installed
when your migration runs.

### No `down()` migrations

Migrations are forward-only. Host state (apt packages, systemd units,
sysctls) rarely reverses cleanly. Rely on idempotency and retry to
converge. If a migration is wrong, write a new migration that fixes it.

### Migration log

Applied migrations are tracked in `/etc/openhost/migrations.jsonl` —
one JSON line per attempt (success or failure). The host's current
version is the highest successful entry. Failed entries are logged but
don't advance the version, so retries pick up from the last success.

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

