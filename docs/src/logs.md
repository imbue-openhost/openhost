## Logs

OpenHost produces several distinct log streams, stored in different places depending on the process.

---

### Router (`compute_space`)

The router writes to two sinks simultaneously via loguru:

**File sink** — `<data_dir>/compute_space.log`

- Written at INFO level and above.
- Truncated on each process restart, so it always contains only the current invocation's logs.
- Rotates automatically at 10 MB mid-run; up to 5 rotated files kept (named `compute_space.YYYY-MM-DD_HH-MM-SS_ffffff.log` alongside the active file). Note: rotation-based retention only fires when the 10 MB limit is actually hit — rotated files from old runs are not pruned until the next mid-run rotation.
- Access via `oh logs` or `oh logs --instance <name>`.

**Stderr sink** — journald (via systemd)

- Written at DEBUG level and above.
- Captured by systemd from the router process's stderr and stored in journald's binary database at `/var/log/journal/`.
- Also contains output from Caddy and CoreDNS, because both are child processes of the router — their stdout is read line-by-line and re-emitted through the router's logger.
- Persists across restarts and accumulates until journald's system-level limits are hit (not configured by OpenHost; system defaults apply — typically 10% of disk or 4 GB, whichever is smaller).
- Access via `journalctl -u openhost` or `journalctl -u openhost -f` to follow.

---

### App build logs

During deploy and reload, `podman build` output and container start/stop events are streamed to a per-app file:

`<temp_data_dir>/app_temp_data/<app_name>/docker.log`

- Archived (not deleted) at the start of each reload. The existing file is renamed with its modification time as a suffix: `docker.log.20240101_120000`. This timestamp reflects when that build ended, not when the next one started.
- Up to 5 archived build logs are kept per app; older ones are deleted when the limit is exceeded.
- Deleted entirely when the app is removed.
- Access via `oh app logs <name>` (shown first, before runtime logs).

---

### App container runtime logs (stdout/stderr)

Container stdout and stderr are written to a per-app file via podman's `k8s-file` log driver:

`<temp_data_dir>/app_temp_data/<app_name>/container.log`

- Capped at 10 MB per file, with up to 3 rotated files kept — so at most 30 MB of runtime logs per app on disk at any time. Rotation is handled by podman automatically.
- Deleted when the app is removed.
- Access via `oh app logs <name>` (shown after build logs, sourced via `podman logs`).
- These logs do **not** appear in journald — the `k8s-file` driver writes directly to disk, bypassing journald entirely.

---

### JuiceFS (`openhost-juicefs.service`)

If an S3 archive backend is configured, JuiceFS runs as a separate systemd unit. Its logs go to journald and can be accessed via:

```
journalctl -u openhost-juicefs
```

---

### systemd / journalctl

`journalctl -u openhost` gives a unified view of everything the router process wrote to stderr, including forwarded Caddy and CoreDNS output. This is the right tool for:

- Diagnosing startup failures (before the file sink is initialised)
- Viewing logs across restarts in one stream
- Checking recent router activity without `oh` access: `systemctl status openhost` shows the last few lines

`journalctl -u openhost` does **not** contain:
- App container stdout/stderr (now written to `container.log` via k8s-file)
- The contents of `compute_space.log` (the file sink is separate from journald)

Journald stores logs in a binary indexed format across multiple files. Queries are indexed by unit, priority, and time — they are not a linear scan — but total journal size still affects seek time. No per-unit size cap is configured by OpenHost; use `SystemMaxUse`, `SystemMaxFiles`, or `MaxRetentionSec` in `/etc/systemd/journald.conf` if the journal grows too large.
