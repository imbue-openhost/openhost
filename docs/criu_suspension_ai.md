# CRIU-based App Suspension — Session Status

**Goal**: Free memory from idle apps by CRIU-checkpointing their state to disk and
restoring on demand, preserving in-memory state (unlike `podman stop`/`start`).

---

## Current status

**Checkpoint: works.** Root podman checkpoint produces a valid ~71K zstd archive
containing `config.dump`, `spec.dump`, `network.status`, and CRIU images under
`checkpoint/`. The archive is suitable for `podman container restore --import`.

**Restore: blocked at final step.** Root podman restore fails because:
1. pasta refuses to run as root ("Don't run as root. Changing to nobody… No child processes")
2. The pixi conmon binary fails when invoked by root ("invalid argument")

### Remaining work to unblock restore

On the server (one-time setup, also needs to go into migration + ansible):

```bash
# 1. Install system conmon (the pixi conmon fails as root)
sudo apt install -y conmon

# 2. Find pasta in the pixi env
PASTA_REAL=$(find /home/host/openhost/.pixi -name pasta -executable | head -1)

# 3. Create pasta wrapper — runs pasta as host user when called by root
sudo tee /usr/local/sbin/pasta << EOF
#!/bin/sh
if [ "\$(id -u)" = "0" ]; then
    exec runuser -u host -- $PASTA_REAL "\$@"
fi
exec $PASTA_REAL "\$@"
EOF
sudo chmod 755 /usr/local/sbin/pasta

# 4. Symlink conmon into the same helper dir
sudo ln -sf /usr/bin/conmon /usr/local/sbin/conmon

# 5. Update /root/.config/containers/containers.conf
sudo tee /root/.config/containers/containers.conf << 'CONF'
[engine]
runtime = "runc"
helper_binaries_dir = ["/usr/local/sbin"]

[engine.runtimes]
runc = ["/usr/local/sbin/openhost-runc"]
CONF
```

Then test:
```bash
sudo /usr/local/bin/openhost-restore /tmp/test-cp3.tar.gz openhost-file-browser && echo "OK"
podman ps | grep file-browser
```

After restore works, test end-to-end:
```bash
oh app suspend file-browser --wait
oh app resume file-browser --wait
oh curl https://file-browser.<domain>/
```

---

## Architecture

### Why root podman is required

Rootless podman hard-blocks CRIU checkpoint/restore at the Go level:
```go
if rootless.IsRootless() { return fmt.Errorf("restoring a container requires root") }
```
This check predates `CAP_CHECKPOINT_RESTORE` (kernel 5.9, 2020), was never updated,
and is not on the podman roadmap. Tested: systemd-run with AmbientCapabilities,
podman unshare, setpriv — all fail. Root podman is the only path. See
[podman#2038](https://github.com/containers/podman/issues/2038).

The rootless security boundary is partially accepted for checkpoint/restore:
sudoers rules scope root access to two specific helper binaries only.

### Why root podman restore needs pasta + conmon fixes

Root podman with `--root /home/host/...` shares the rootless user's container storage
but invokes helper binaries (conmon, pasta) as root. Both refuse root:
- **pasta**: "Don't run as root" — intentional upstream behaviour.
  Fix: wrapper that `runuser -u host` before exec.
- **conmon**: the pixi-bundled conmon binary fails with "invalid argument" as root.
  Fix: system conmon from apt, found via `helper_binaries_dir`.

### Checkpoint flow (working)

```
compute_space (rootless)
  → podman inspect → get full container ID
  → sudo /usr/local/bin/openhost-checkpoint <id> <archive_path>
      → root podman --root /home/host/.local/share/containers/storage
                    --runroot /run/user/<uid>/containers
                    container checkpoint --export <path> --ignore-rootfs <id>
      → trap EXIT: find -not -user host -exec chown host:host
```

### Restore flow (almost working)

```
compute_space (rootless)
  → podman rm -f <name>   (remove stopped container from rootless DB)
  → sudo /usr/local/bin/openhost-restore <archive_path> <container_name>
      → root podman rm -f <name>  (remove stale root podman DB entry)
      → root podman --root ... --runroot ...
                    container restore --import <path>
        (needs: /usr/local/sbin/pasta wrapper + /usr/local/sbin/conmon symlink)
      → trap EXIT: chown cleanup
```

### Key technical discoveries

- **runroot must match the DB-recorded value**: podman stores runroot in its state DB.
  Passing a temp runroot produces "database configuration mismatch". Always use
  `/run/user/<uid>/containers`.
- **chown must use trap**: `set -e` exits on restore failure, skipping cleanup.
  Without trap, root-owned files remain and break subsequent rootless podman operations.
- **`--conmon` flag is deprecated**: newer podman ignores it; use `conmon_path` in
  containers.conf or `helper_binaries_dir`.
- **Archive is zstd not gzip**: podman `--export` creates Zstandard archives. Use
  `tar tf` (not `tar tzf`) to inspect; `tar -xOf file.tar.gz config.dump` fails if
  tar detects `.gz` extension and tries gzip.
- **conda-forge runc is actually crun**: the pixi `runc` binary is crun under a
  different name, no CRIU support. `apt install runc` provides real opencontainers
  runc at `/usr/sbin/runc`. Host containers.conf maps "runc" → `/usr/sbin/runc`.
- **Root runc state path**: rootless runc stores state at `/run/user/<uid>/runc/`.
  Root runc defaults to `/run/runc/`. `openhost-runc` wrapper injects
  `--root /run/user/<uid>/runc` so root podman can find rootless container state.
- **"that ID is already in use"**: after checkpoint, root podman retains a DB entry.
  Rootless `podman rm -f` may fail to clean root-owned files. Fix: root podman
  `rm -f <name>` inside the restore helper before restoring.

---

## Files changed

| File | What changed |
|------|-------------|
| `compute_space/src/compute_space/core/containers.py` | Added `checkpoint_container()`, `restore_container()` |
| `openhost_system_agent/src/openhost_system_agent/migrations/versions/v0003_swap_and_criu.py` | New migration: installs CRIU, swap, helpers, sudoers, containers.conf |
| `openhost_system_agent/src/openhost_system_agent/migrations/registry.py` | Registered v0003 |
| `ansible/tasks/swap.yml` | Swap + CRIU setup for fresh provisioning |
| `ansible/templates/openhost.service.j2` | Added `AmbientCapabilities=CAP_CHECKPOINT_RESTORE` |

### Helper scripts installed by migration / ansible

| Path | Purpose |
|------|---------|
| `/usr/local/sbin/criu` | CRIU binary (built from source v4.1) |
| `/usr/local/bin/criu` | Wrapper: injects `--unprivileged` for `criu check` when rootless |
| `/usr/local/sbin/openhost-runc` | runc wrapper: injects `--root /run/user/<uid>/runc` |
| `/usr/local/bin/openhost-checkpoint` | Root podman checkpoint + trap chown cleanup |
| `/usr/local/bin/openhost-restore` | Root podman rm-then-restore + trap chown cleanup |
| `/etc/sudoers.d/openhost-checkpoint` | Allows `host` to sudo the two helpers (NOPASSWD) |
| `/home/host/.config/containers/containers.conf` | Points "runc" → `/usr/sbin/runc` (real runc, not crun) |
| `/root/.config/containers/containers.conf` | Points "runc" → `openhost-runc` wrapper |

### Migration also needs (not yet in code, needed for pasta/conmon fix)

- `apt install conmon`
- `/usr/local/sbin/pasta` wrapper script (run pasta as host user)
- `/usr/local/sbin/conmon` → `/usr/bin/conmon` symlink
- Update root containers.conf to include `helper_binaries_dir = ["/usr/local/sbin"]`

---

## After restore works — remaining tasks

1. Wire up `suspend` / `resume` API endpoints in compute_space (check if they
   already exist; if not, add HTTP handlers calling `checkpoint_container` /
   `restore_container`)
2. Run tests: `pixi run -e dev pytest -x`
3. Commit all changes
4. Update migration to include pasta wrapper + conmon install
5. Update ansible swap.yml to match migration
