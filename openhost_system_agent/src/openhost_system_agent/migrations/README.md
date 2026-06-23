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
- `migration_tags.json` is re-read at each step, so its format can evolve.
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

3. Add an entry to `migration_tags.json` (in this directory):
```json
{"git_tag_before_migration": "v1.2.0", "migration": {NNNN}}
```
`git_tag_before_migration` is the most recent semver tag *before* this
migration was written. Set `null` if no tags exist yet.

4. Migrations must be **idempotent** — safe to re-run if a previous
   attempt failed partway through and is retried.

5. Versions must be **contiguous** starting at 2 (v1 is the ansible
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
- `migration_tags.json` is re-read at each step from the current
  checkout, so its format can change between tags.
- Migrations with `git_tag_before_migration = T` were written assuming
  the host is at state >= T. By construction, all prior tags have been
  applied before T's migrations run.

### The invariant to maintain

**All migrations written against tag T must be merged before the next
tag is cut.** If a migration misses its window, bump its
`git_tag_before_migration` in `migration_tags.json` before merging.

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

### The bootstrap release

The re-exec handoff, phased migrations, and tag-based updates didn't
exist in the original codebase. The release that introduces them must
be installable by the old update mechanism (old pixi, old `apply_update`).
After that one release, all future updates go through the new system.

Concretely: the first release ships with a lockfile the old fleet's pixi
can read. Once deployed, the pixi upgrade migration brings the fleet
forward, and subsequent releases can use the new lockfile format.

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

### The `apply_after_checkout.py` stability contract

`apply_after_checkout.py` is the interface between old and new code.
After checkout, the prior tag's `_reexec_apply` runs the *new*
checkout's copy of this file. Its file path, argv interface, and stdout
JSON shape must stay compatible with the prior tag's caller. Once a new
tag is cut the contract resets — the new tag's `_reexec_apply` becomes
the caller. So you can change this file freely as long as no untagged
release is still the "prior tag" for any host in the fleet.
