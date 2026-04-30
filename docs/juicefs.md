# JuiceFS-backed archive tier

This is operator-facing.  App developers don't need to read it; their
manifest just says `[data] app_archive = true` and the bytes flow to
whichever backing the operator picked.

## What it does

Replaces the default local-disk backing for the `app_archive` tier
with a [JuiceFS](https://juicefs.com/docs/community/introduction)
mount, which puts every byte under `/data/app_archive/<app>/` on
the operator's S3 bucket.  Apps see the same in-container path
either way; only the host-side storage changes.

This is useful when zone storage outgrows the local disk and the
operator doesn't want to resize the VM.  For most zones the local
fallback is fine.

## What it doesn't do

- Replace `app_data`.  SQLite, advisory-lock-using apps, and
  anything mmap-shared still lives on local disk.  The manifest
  validator rejects an `app_archive`-only manifest exactly
  because of this.
- Span multiple openhost VMs.  The metadata engine is single-host
  SQLite.  Multi-host JuiceFS would need Redis or Postgres for
  metadata; that's not the failure mode this design targets.
- Encrypt at rest, beyond what S3 server-side encryption provides.
  Operators who need additional encryption should use
  `juicefs format --encrypt-rsa-key=…` (not yet wired through the
  ansible role).

## Enabling it

Pass `juicefs_enabled=true` plus the S3 details to ansible:

```bash
ansible-playbook ansible/setup.yml -i <ip>, \
  -e domain=<domain> \
  -e juicefs_enabled=true \
  -e juicefs_s3_bucket=my-openhost-archive \
  -e juicefs_s3_region=us-east-1 \
  -e juicefs_s3_access_key=AKIA… \
  -e juicefs_s3_secret_key=… \
  -e juicefs_volume_name=openhost
```

Optional: `juicefs_s3_endpoint=https://…` for non-AWS S3
(MinIO, Backblaze B2, etc.).

The role:
1. Installs the pinned JuiceFS binary (`v1.3.1`) with sha256 verify.
2. `juicefs format` against your bucket, idempotent via a sentinel
   file at `/var/lib/juicefs/.formatted`.
3. Installs `juicefs-mount.service` and makes `openhost.service` a
   hard `Requires=` dependent on it (not just ordered after).  If the
   mount unit fails or dies, systemd refuses to start openhost; apps
   can never silently write to the underlying empty mount-point and
   have those writes get shadowed once the mount comes back.
4. Installs `juicefs-meta-dump.service` + a 24h timer that dumps the
   metadata SQLite to a JSON inside `persistent_data_dir/openhost/`
   so the existing `openhost-backup` (restic) app picks it up.

## Disaster recovery

If the openhost VM is lost, the S3 bucket retains the data but
without the metadata you can't reattach.  The metadata-dump timer
covers this:

1. Provision a fresh openhost VM with `juicefs_enabled=true` and the
   SAME S3 bucket + same volume name as before.  The format step
   detects the existing volume in S3 and is a no-op.
2. Restore `juicefs-metadata-dump.json` from the most recent restic
   snapshot to its original path.
3. `sudo -u host juicefs load sqlite3:///var/lib/juicefs/meta.db
    /home/host/.openhost/local_compute_space/persistent_data/openhost/juicefs-metadata-dump.json`.
4. `sudo systemctl restart juicefs-mount.service openhost.service`.

The dump runs every 24h (with up-to-15min jitter) so the worst-case
loss window is ~24h.  Operators who need tighter RPO can tighten the
timer.

## Operational notes

- Secrets live at `/etc/openhost/juicefs/s3.env` mode 0640 root:host;
  they are not visible in `ps`.
- Mount logs land at `/var/log/juicefs/mount.log`.  The role
  pre-creates `/var/log/juicefs/` owned by `host` so the unit (which
  runs as `host`) can write there directly.  `journalctl -u
  juicefs-mount.service` shows the systemd-side stdout/stderr.
- `juicefs status sqlite3:///var/lib/juicefs/meta.db` shows volume
  health.  Run as `host`.
- Disabling JuiceFS later: re-run the playbook with
  `juicefs_enabled=false`.  The role doesn't run, the
  `archive_dir_override` line is removed from `config.toml`, and
  apps fall back to local disk on the next openhost restart.  The
  S3 bucket's contents stay; reverting per-app data from S3 to
  local disk is a manual `juicefs sync` operation outside the role.
