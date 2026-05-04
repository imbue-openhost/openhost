"""Operator-controlled archive backend management.  See
``docs/data.md`` for the operator-facing model."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

import attr

from compute_space.config import Config
from compute_space.core.logging import logger


# ---------------------------------------------------------------------------
# JuiceFS install + mount machinery
# ---------------------------------------------------------------------------

# Pin to a specific JuiceFS release; checksums verified before extract
# so a compromised release page can't swap the tarball.  Bump this
# tuple to upgrade.
JUICEFS_VERSION = "1.3.1"
JUICEFS_SHA256 = {
    "amd64": "eb67a7be5d174b420cb3734d441971b3a462ab522b78ad2a6ed993e7deddcd44",
    "arm64": "c29bff8f609366011cee03b9abcc76c11a06308b2c314364b8c340a2bfbc6c48",
}


def _arch() -> str:
    """Return the JuiceFS-release-asset arch string for the running host."""
    machine = os.uname().machine
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


# ---------------------------------------------------------------------------
# On-disk layout (host)
#
# All JuiceFS-related files openhost-core writes go under one of these
# two directories, both under the host user's data tree (no sudo).
# Splitting by lifetime + criticality is what lets an operator answer
# the "what do I have to back up" question without reading the code:
#
#   <openhost_data_path>/juicefs/state/      <- ``_juicefs_state_dir``
#                                               CRITICAL.  Must be
#                                               backed up.  Losing
#                                               this means the S3
#                                               bucket is recoverable
#                                               only via JuiceFS's
#                                               periodic meta-dump
#                                               replays in S3.
#       meta.db                                  <- the SQLite metadata DB
#                                                  (file -> chunk-id mapping)
#
#   <openhost_data_path>/juicefs/runtime/    <- ``_juicefs_runtime_dir``
#                                               REGENERABLE.  Safe to
#                                               drop on any boot — the
#                                               install/mount code
#                                               recreates it on demand.
#       bin/juicefs-<version>                    <- the JuiceFS binary
#                                                  (re-downloaded if missing)
#
# ``juicefs_mount_dir`` (the FUSE mount point) lives under
# ``data_root_dir/`` so it isn't included in the existing restic-based
# host backups — JuiceFS's bucket already holds those bytes and
# double-backing them up wastes time and storage.
# ---------------------------------------------------------------------------


def _juicefs_state_dir(config: Config) -> str:
    """Critical-state directory.

    Holds files that MUST survive across reboots and that are not
    automatically reconstructible from the S3 bucket (the mappings
    from filenames to bucket objects).  An operator who wants to
    survive disk loss should snapshot this directory regularly.
    """
    return os.path.join(config.openhost_data_path, "juicefs", "state")


def _juicefs_runtime_dir(config: Config) -> str:
    """Regenerable-state directory.

    Holds files openhost-core (re)creates on demand: the JuiceFS
    binary, future flock files, JuiceFS's own log dir if we ever
    redirect it.  Safe to wipe.
    """
    return os.path.join(config.openhost_data_path, "juicefs", "runtime")


def _juicefs_install_dir(config: Config) -> str:
    """Where the JuiceFS binary lives.  ``runtime/bin/`` under the
    juicefs tree because the binary is regenerable on boot."""
    return os.path.join(_juicefs_runtime_dir(config), "bin")


def _juicefs_binary(config: Config) -> str:
    return os.path.join(_juicefs_install_dir(config), f"juicefs-{JUICEFS_VERSION}")


def _juicefs_meta_db(config: Config) -> str:
    """SQLite metadata DB for JuiceFS.  Lives under the critical-
    state directory so the existing backup flow picks it up — that's
    what lets a fresh VM with the same S3 bucket reattach via the
    metadata.
    """
    return os.path.join(_juicefs_state_dir(config), "meta.db")


# Legacy paths used by openhost-core before the juicefs/state +
# juicefs/runtime split.  ``_migrate_legacy_layout`` renames any of
# these into their new homes once on startup.  Kept as module-level
# constants (not helper functions) because they're only referenced
# from one place and inlining the join would be three identical lines.
_LEGACY_META_DB_BASENAME = "juicefs-meta.db"
_LEGACY_INSTALL_DIRNAME = "juicefs"


def _migrate_legacy_layout(config: Config) -> None:
    """Move a pre-tidy meta DB into the new ``juicefs/state/`` home.

    Idempotent: if the new path already exists or the legacy path
    doesn't, this is a no-op.  Runs as part of every JuiceFS install
    on every boot so an operator who upgrades from a pre-tidy build
    never has to do anything manual.

    The legacy install dir (``<openhost_data_path>/juicefs/``) collides
    with the new top-level ``juicefs/`` dir we're about to use.  We
    rename the legacy install dir AWAY first if it has the legacy
    binary at the top level, before creating the new layout below it.
    """
    legacy_meta = os.path.join(config.openhost_data_path, _LEGACY_META_DB_BASENAME)
    new_meta = _juicefs_meta_db(config)
    legacy_install = os.path.join(config.openhost_data_path, _LEGACY_INSTALL_DIRNAME)

    # Step 1: if the legacy install dir is the OLD shape (binaries
    # at its root), shuffle them aside so we can lay the new
    # state/runtime tree underneath.  ``_LEGACY_INSTALL_DIRNAME`` is
    # the same directory name we use today for the ``juicefs/`` parent
    # of state/ + runtime/, so we have to disambiguate by checking
    # whether anything that looks like the legacy binary lives at the
    # top level rather than under ``runtime/bin/``.
    if os.path.isdir(legacy_install):
        legacy_top_level_files = []
        try:
            for entry in os.scandir(legacy_install):
                if entry.is_file() and entry.name.startswith("juicefs-"):
                    legacy_top_level_files.append(entry.path)
        except OSError:
            legacy_top_level_files = []
        if legacy_top_level_files:
            # Move each binary into the new runtime/bin/ home.
            new_bin_dir = _juicefs_install_dir(config)
            os.makedirs(new_bin_dir, exist_ok=True)
            for src in legacy_top_level_files:
                dst = os.path.join(new_bin_dir, os.path.basename(src))
                if not os.path.exists(dst):
                    try:
                        os.rename(src, dst)
                        logger.info(
                            "Migrated legacy juicefs binary %s -> %s", src, dst
                        )
                    except OSError as exc:
                        # Non-fatal: ``install_juicefs`` will redownload
                        # if the binary is still missing at the new
                        # path.  Logging keeps the failure visible.
                        logger.warning(
                            "Could not migrate legacy juicefs binary %s -> %s: %s",
                            src, dst, exc,
                        )

    # Step 2: rename the meta DB from the openhost_data_path root to
    # the new state dir.  This is the file an operator MUST not lose
    # quietly, so be loud on success and louder on failure.
    if os.path.isfile(legacy_meta) and not os.path.exists(new_meta):
        os.makedirs(os.path.dirname(new_meta), exist_ok=True)
        try:
            os.rename(legacy_meta, new_meta)
            logger.info(
                "Migrated JuiceFS metadata DB %s -> %s (one-shot layout tidy)",
                legacy_meta, new_meta,
            )
        except OSError as exc:
            # Refusing to silently continue: the meta DB is the one
            # file whose loss makes the bucket unreadable.  An
            # operator who sees an in-flight rename failure should
            # fix it manually before the next boot, not have a
            # second copy mysteriously appear at the new path.
            raise RuntimeError(
                f"Could not migrate JuiceFS metadata DB from {legacy_meta!r} "
                f"to {new_meta!r}: {exc}.  Refusing to start the archive "
                f"backend — fix the rename manually (or let the original "
                f"meta DB be the source of truth) before retrying."
            ) from exc


def juicefs_mount_dir(config: Config) -> str:
    """The host-side directory where the JuiceFS mount lives.  Per-app
    subdirs underneath are bind-mounted into containers at
    ``/data/app_archive/<app>/`` (see ``run_container``).
    """
    # Lives under data_root_dir so all openhost state is in one tree.
    # NOT under persistent_data_dir, because we don't want this path
    # included in restic backups (the bytes are already in S3).
    return os.path.join(config.data_root_dir, "app_archive_juicefs")


def juicefs_meta_db_path(config: Config) -> str:
    """Public alias of ``_juicefs_meta_db`` for the route layer.

    The dashboard surfaces this path so an operator who wants to
    snapshot the must-back-up file knows exactly where to look.
    Not part of the BackendState DTO because it's a derived path,
    not persistent DB state.
    """
    return _juicefs_meta_db(config)


def juicefs_state_dir(config: Config) -> str:
    """Public alias of ``_juicefs_state_dir`` for the route layer."""
    return _juicefs_state_dir(config)


def is_juicefs_installed(config: Config) -> bool:
    return os.path.isfile(_juicefs_binary(config)) and os.access(
        _juicefs_binary(config), os.X_OK
    )


def install_juicefs(config: Config) -> None:
    """Download + verify + extract the JuiceFS binary.  Idempotent.

    Lives under the host user's data tree so no sudo is needed; the
    binary is per-version-suffixed so a future upgrade can install the
    new one alongside the old before swapping a symlink (we don't ship
    that flow today — bumping JUICEFS_VERSION just makes a fresh
    install on the next boot).

    Runs the legacy-layout migration first so a zone whose meta DB
    still lives at the pre-tidy path gets renamed into the new
    ``juicefs/state/`` home before the install proceeds.  Idempotent
    on already-migrated zones; cheap enough to run on every install
    that openhost-core won't notice the extra ``os.path.isfile`` calls.
    """
    _migrate_legacy_layout(config)
    if is_juicefs_installed(config):
        return
    install_dir = _juicefs_install_dir(config)
    os.makedirs(install_dir, exist_ok=True)
    arch = _arch()
    expected_sha = JUICEFS_SHA256.get(arch)
    if not expected_sha:
        raise RuntimeError(
            f"No pinned JuiceFS sha256 for arch {arch!r}; refusing to install."
        )
    url = (
        f"https://github.com/juicedata/juicefs/releases/download/"
        f"v{JUICEFS_VERSION}/juicefs-{JUICEFS_VERSION}-linux-{arch}.tar.gz"
    )
    logger.info("Downloading JuiceFS %s for %s", JUICEFS_VERSION, arch)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            tarball_bytes = resp.read()
    except (urllib.error.URLError, socket.timeout) as exc:
        raise RuntimeError(f"Failed to download JuiceFS: {exc}") from exc

    actual_sha = hashlib.sha256(tarball_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"JuiceFS tarball sha256 mismatch (expected {expected_sha}, "
            f"got {actual_sha}).  Refusing to install."
        )

    # Extract just the ``juicefs`` binary; ignore the LICENSE / README
    # the tarball ships alongside.  ``tarfile`` is safer than shelling
    # out to ``tar`` when the input is opaque bytes.
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == "juicefs"), None)
        if member is None:
            raise RuntimeError("JuiceFS tarball missing the ``juicefs`` binary")
        # ``extractfile`` returns None for non-regular tar entries;
        # check before entering the context manager so we surface a
        # clear RuntimeError instead of an AttributeError on __enter__.
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError("JuiceFS tarball entry was unreadable")
        binary_path = _juicefs_binary(config)
        with f, open(binary_path, "wb") as out:
            shutil.copyfileobj(f, out)
    os.chmod(_juicefs_binary(config), 0o750)
    logger.info("JuiceFS installed at %s", _juicefs_binary(config))


def _format_meta_dsn(config: Config) -> str:
    return f"sqlite3://{_juicefs_meta_db(config)}"


def _bucket_url(
    s3_bucket: str,
    s3_region: str,
    s3_endpoint: str | None,
) -> str:
    """JuiceFS expects the bucket as a URL even on AWS.  Custom S3
    endpoints (MinIO etc.) need the explicit endpoint; AWS gets the
    region-suffixed virtual-host URL.

    No path component is appended.  Per-zone isolation under a shared
    bucket happens via ``juicefs_volume_name`` (which JuiceFS uses as
    the prefix for every object it writes), NOT via path segments on
    the bucket URL — JuiceFS's S3 backend parses the URL with
    ``url.ParseRequestURI`` and treats the first path component as
    the bucket name (see pkg/object/s3.go), so any extra path here
    would silently get reinterpreted as the bucket and break the
    DNS lookup.  Keep the URL clean.
    """
    if s3_endpoint:
        return f"{s3_endpoint.rstrip('/')}/{s3_bucket}"
    return f"https://{s3_bucket}.s3.{s3_region or 'us-east-1'}.amazonaws.com"


def format_volume(
    config: Config,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str,
) -> None:
    """Run ``juicefs format`` against the S3 bucket.  Idempotent.

    ``juicefs format`` is a no-op when the volume already exists in the
    bucket with the same params (it just refreshes the local metadata).
    Running it on every backend-on switch is therefore safe and means
    the recovery flow ("provision a fresh VM with the same bucket") is
    the same code path as the first-time setup.

    ``juicefs_volume_name`` doubles as the per-zone object prefix:
    every chunk JuiceFS writes lands under ``<bucket>/<volume>/...``
    (see pkg/object/object_storage.go's ``WithPrefix`` wiring in
    cmd/format.go).  The route layer takes the operator's
    ``s3_prefix`` form field and passes it here as the volume name
    when set, which is how zone-A and zone-B can share a bucket
    without colliding.
    """
    bucket_url = _bucket_url(s3_bucket, s3_region or "us-east-1", s3_endpoint)
    cmd = [
        _juicefs_binary(config),
        # JuiceFS opens a Go pprof debug HTTP server on 127.0.0.1:6060
        # by default for every command, and walks 6061..6099 if 6060
        # is already taken.  We disable it here for the same reason
        # we disable it on ``mount`` (see the long comment there) —
        # rarely useful in production, and on this short-lived
        # synchronous call we run-watch-forget, the host gets briefly
        # contaminated with a transient 6060 listener that the
        # security-audit flags as ``unexpected`` for the duration of
        # the format.
        "--no-agent",
        "format",
        "--storage", "s3",
        "--bucket", bucket_url,
        _format_meta_dsn(config),
        juicefs_volume_name,
    ]
    # juicefs format reads ACCESS_KEY / SECRET_KEY from env when the
    # --access-key / --secret-key flags aren't passed.  Using env
    # keeps the secret out of ``ps`` and any process-listing tooling
    # that would otherwise pick it up.
    env = os.environ.copy()
    env["ACCESS_KEY"] = s3_access_key_id
    env["SECRET_KEY"] = s3_secret_access_key
    logger.info("Running juicefs format against %s", bucket_url)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"juicefs format failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# Mount supervision
# ---------------------------------------------------------------------------


_mount_lock = threading.Lock()
_mount_proc: subprocess.Popen[bytes] | None = None


def is_mounted(mount_point: str) -> bool:
    """Return True iff ``mount_point`` is a mount point right now.

    /proc/self/mountinfo is the source of truth; ``os.path.ismount``
    works but breaks on some FS/userns combinations.
    """
    try:
        with open("/proc/self/mountinfo", "r") as f:
            for line in f:
                # Each line: "id parent maj:min root mount_point ..."
                parts = line.split()
                if len(parts) >= 5 and parts[4] == mount_point:
                    return True
    except OSError:
        return False
    return False


def mount(
    config: Config,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> None:
    """Start ``juicefs mount`` as a child process so it inherits
    openhost-core's lifecycle.  Idempotent: if the mount is already
    up, no-ops.

    JuiceFS mount runs in the foreground by default (no flag needed);
    we leave it that way rather than going through a systemd unit so
    we don't need root or a sudoers carve-out.  openhost.service
    already restarts on crash; if openhost-core dies the mount goes
    with it, which is the right semantics — apps mid-archive-write
    would see the mount drop, the supervisor restarts everything, and
    the mount comes back at the same path.
    """
    global _mount_proc
    mount_point = juicefs_mount_dir(config)
    os.makedirs(mount_point, exist_ok=True)

    with _mount_lock:
        if is_mounted(mount_point):
            logger.info("juicefs already mounted at %s", mount_point)
            return
        env = os.environ.copy()
        # Creds out of argv to keep them out of ``ps``.
        env["ACCESS_KEY"] = s3_access_key_id
        env["SECRET_KEY"] = s3_secret_access_key
        cmd = [
            _juicefs_binary(config),
            # Disable JuiceFS's pprof debug HTTP agent.  Mount actually
            # spawns *two* juicefs processes (a stage-0 supervisor +
            # the stage-3 daemon, see the daemon-stage logic in
            # cmd/mount.go upstream), each of which would otherwise
            # call setup() and bind 127.0.0.1:6060 / :6061 — so even
            # though our route only invokes ``juicefs mount`` once,
            # two extra ports show up in the security-audit's
            # listening-ports view as ``unexpected``.  The agent is
            # rarely useful in production and ``--no-agent`` is the
            # cleanest way to remove the audit-noise + the unbounded
            # 6060..6099 port-walking that JuiceFS's main.go does
            # when those ports are already taken by something else.
            "--no-agent",
            "mount",
            "--no-usage-report",
            _format_meta_dsn(config),
            mount_point,
        ]
        logger.info("Starting juicefs mount at %s", mount_point)
        # ``stdout`` and ``stderr`` go to DEVNULL because juicefs
        # mount is long-lived and will fill a 64 KiB pipe buffer if
        # we don't continuously drain it, which would freeze the
        # mount.  juicefs already writes its own log file; the bits
        # we'd lose to DEVNULL are duplicated there.
        _mount_proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait up to 15 s for the mount to register in mountinfo.
        deadline = time.time() + 15
        while time.time() < deadline:
            if is_mounted(mount_point):
                logger.info("juicefs mount ready at %s", mount_point)
                return
            rc = _mount_proc.poll()
            if rc is not None:
                # Process exited without mounting.  Reap it and
                # surface the failure.  juicefs writes its own log to
                # ~/.juicefs/juicefs.log by default; we don't override
                # that, so check there for the underlying error.
                _mount_proc = None
                raise RuntimeError(
                    f"juicefs mount exited early (rc={rc}); check ~/.juicefs/juicefs.log"
                )
            time.sleep(0.2)
        # Timeout: the child is still alive but hasn't registered a
        # mount.  Kill it before raising so we don't leak a process
        # that holds the mount-point lock and prevents a retry.
        try:
            _mount_proc.terminate()
            try:
                _mount_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _mount_proc.kill()
                _mount_proc.wait(timeout=5)
        except Exception:
            logger.exception("Failed to terminate stuck juicefs mount process")
        finally:
            _mount_proc = None
        raise RuntimeError(
            f"juicefs mount did not become ready within 15s at {mount_point}"
        )


def umount(config: Config) -> None:
    """Unmount the JuiceFS mount and reap the supervised process.

    Calls ``juicefs umount`` once; if that fails (typically because
    the FS is busy from a still-running container), surfaces an
    error rather than swallowing the failure.  Lazy unmount via
    ``umount -l`` would handle the busy case but it requires root,
    which we deliberately don't have — the operator-facing dashboard
    is supposed to stop affected apps before triggering a backend
    switch, so the busy case shouldn't fire on the happy path.
    Idempotent on already-unmounted state (returns cleanly).
    """
    global _mount_proc
    mount_point = juicefs_mount_dir(config)

    with _mount_lock:
        if not is_mounted(mount_point):
            if _mount_proc is not None and _mount_proc.poll() is None:
                _mount_proc.terminate()
                try:
                    _mount_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _mount_proc.kill()
                    try:
                        _mount_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # If even SIGKILL + 5 s wait doesn't reap it,
                        # something is very wrong.  Drop the handle so
                        # we don't reuse it and let the OS clean up.
                        logger.error(
                            "juicefs mount process did not exit after SIGKILL"
                        )
            _mount_proc = None
            return
        cmd = [_juicefs_binary(config), "umount", mount_point]
        # Always clear ``_mount_proc`` on any exit path so a retry
        # doesn't inherit a stale handle pointing at a process whose
        # state is unknown.  Reaping (kill+wait) the supervised mount
        # process happens inside the try/finally so a TimeoutExpired
        # from subprocess.run still triggers the cleanup.
        try:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30
                )
            except subprocess.TimeoutExpired as exc:
                # The juicefs umount binary itself hung.  Treat as a
                # busy-FS failure and surface the error.
                raise RuntimeError(
                    f"juicefs umount of {mount_point} timed out after 30s"
                ) from exc
            if result.returncode != 0:
                raise RuntimeError(
                    f"juicefs umount of {mount_point} failed "
                    f"(rc={result.returncode}); ensure all containers "
                    f"using the archive tier are stopped before switching "
                    f"backends.  Original: {result.stderr.strip()}"
                )
            logger.info("juicefs unmounted from %s", mount_point)
        finally:
            # Reap the supervised mount process.  juicefs umount tells
            # the FUSE process to exit cleanly; wait briefly for it to
            # do so, then SIGKILL + wait if it didn't.  Either way,
            # null the global so a subsequent mount() doesn't think a
            # stale handle is live.
            if _mount_proc is not None:
                try:
                    _mount_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _mount_proc.kill()
                    try:
                        _mount_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.error(
                            "juicefs mount process did not exit after SIGKILL"
                        )
            _mount_proc = None


# ---------------------------------------------------------------------------
# Backend state (DB read/write)
# ---------------------------------------------------------------------------


@attr.s(auto_attribs=True, frozen=True)
class BackendState:
    """Operator-visible archive backend state."""

    backend: str  # "local" | "s3"
    state: str  # "idle" | "switching"
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    # Operator-supplied prefix under the bucket so multiple zones
    # can share one bucket cleanly.  ``None`` / empty means
    # "use the bucket root".
    s3_prefix: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    juicefs_volume_name: str
    last_switched_at: str | None
    state_message: str | None


def read_state(db: sqlite3.Connection) -> BackendState:
    row = db.execute(
        "SELECT backend, state, s3_bucket, s3_region, s3_endpoint, "
        "s3_access_key_id, s3_secret_access_key, juicefs_volume_name, "
        "last_switched_at, state_message, s3_prefix FROM archive_backend WHERE id = 1"
    ).fetchone()
    if row is None:
        # Should never happen — the v5 migration seeds this row — but
        # tolerate it so the dashboard doesn't crash on a partial DB.
        return BackendState(
            backend="local",
            state="idle",
            s3_bucket=None,
            s3_region=None,
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id=None,
            s3_secret_access_key=None,
            juicefs_volume_name="openhost",
            last_switched_at=None,
            state_message=None,
        )
    return BackendState(
        backend=row[0],
        state=row[1],
        s3_bucket=row[2],
        s3_region=row[3],
        s3_endpoint=row[4],
        s3_access_key_id=row[5],
        s3_secret_access_key=row[6],
        juicefs_volume_name=row[7] or "openhost",
        last_switched_at=row[8],
        state_message=row[9],
        s3_prefix=row[10],
    )


def _update_state(
    db: sqlite3.Connection,
    *,
    state: str | None = None,
    state_message: str | None = None,
    backend: str | None = None,
    s3_bucket: str | None = None,
    s3_region: str | None = None,
    s3_endpoint: str | None = None,
    s3_prefix: str | None = None,
    s3_access_key_id: str | None = None,
    s3_secret_access_key: str | None = None,
    juicefs_volume_name: str | None = None,
    last_switched_at: str | None = None,
    clear_s3_credentials: bool = False,
) -> None:
    """Update the single archive_backend row.

    ``None`` means "don't update this field" rather than "set to NULL"
    so callers can pass only the fields they care about.  Two
    deliberate deviations:

    - ``state_message`` writes whenever EITHER ``state`` OR
      ``state_message`` is passed.  Combined with the "None means
      skip" rule, this means ``_update_state(db, state='idle')``
      clears any stale message left by a prior switch step,
      and ``_update_state(db, state_message='Stopping apps')``
      writes the message without changing state.
    - ``clear_s3_credentials=True`` explicitly NULLs the access key
      and secret access key columns.  This is how the s3->local
      transition drops the secrets it no longer needs.
    """
    fields: dict[str, object | None] = {}
    if state is not None:
        fields["state"] = state
    if state_message is not None or state is not None:
        # Clear stale state_message whenever we transition state.
        fields["state_message"] = state_message
    if backend is not None:
        fields["backend"] = backend
    if s3_bucket is not None:
        fields["s3_bucket"] = s3_bucket
    if s3_region is not None:
        fields["s3_region"] = s3_region
    if s3_endpoint is not None:
        fields["s3_endpoint"] = s3_endpoint
    if s3_prefix is not None:
        fields["s3_prefix"] = s3_prefix
    if s3_access_key_id is not None:
        fields["s3_access_key_id"] = s3_access_key_id
    if s3_secret_access_key is not None:
        fields["s3_secret_access_key"] = s3_secret_access_key
    if juicefs_volume_name is not None:
        fields["juicefs_volume_name"] = juicefs_volume_name
    if last_switched_at is not None:
        fields["last_switched_at"] = last_switched_at
    if clear_s3_credentials:
        # Switching back to ``local`` should drop the S3 credentials so
        # we don't leave them lying in the DB beyond their useful life.
        # The bucket / region / endpoint stay, so the operator's
        # next-switch-back-to-S3 form is pre-filled with the previous
        # bucket — convenient and not sensitive.
        fields["s3_access_key_id"] = None
        fields["s3_secret_access_key"] = None

    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE archive_backend SET {set_clause} WHERE id = 1",
        list(fields.values()),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Match TOML's ``key = true`` allowing whitespace + a value of true.
# Used by the api routes (rename/reload/add) and by the switch-flow's
# affected-app enumeration to decide which apps "use" the archive
# tier.  Substring matching ("app_archive" in raw + "true" in raw)
# false-matches manifests with ``app_archive = false`` alongside any
# other ``= true`` field, so we anchor on TOML key=value shape.
_MANIFEST_USES_ARCHIVE_RE = re.compile(
    r"(?m)^\s*(?:app_archive|access_all_data)\s*=\s*[Tt][Rr][Uu][Ee]\b"
)


def manifest_uses_archive(manifest_raw: str) -> bool:
    """Return True iff the (raw TOML) manifest opts the app into the
    archive tier — either via ``app_archive = true`` or via
    ``access_all_data = true``.
    """
    return bool(_MANIFEST_USES_ARCHIVE_RE.search(manifest_raw))


def is_archive_dir_healthy(config: Config, db: sqlite3.Connection) -> bool:
    """Return True iff the currently-configured archive backing is
    actually live on the host.

    For ``local``: the per-zone fallback dir under
    ``persistent_data_dir/app_archive/`` simply needs to exist as a
    directory.
    For ``s3``: the JuiceFS mount point must be a live mount, NOT
    just a directory.  ``os.path.isdir`` would return True for the
    underlying empty mount-point even when JuiceFS has dropped,
    silently letting writes go to local disk where they get
    shadowed if the mount comes back; ``is_mounted`` reads
    /proc/self/mountinfo and returns True only when the FS is up.
    """
    state = read_state(db)
    if state.backend == "s3":
        return is_mounted(juicefs_mount_dir(config))
    return os.path.isdir(os.path.join(config.persistent_data_dir, "app_archive"))


def archive_dir_for_backend(config: Config, backend: str) -> str:
    """Return the host-side archive root for ``backend``.

    Used by ``apply_backend_to_config`` to compute the right
    ``archive_dir_override`` value after a switch, and by the switch
    flow itself to know where to copy data to/from.
    """
    if backend == "s3":
        return juicefs_mount_dir(config)
    return os.path.join(config.persistent_data_dir, "app_archive")


def apply_backend_to_config(config: Config, db: sqlite3.Connection) -> Config:
    """Return a Config whose ``archive_dir_override`` matches the
    backend currently recorded in the DB.

    Called once at startup and again after every backend switch so
    that ``config.app_archive_dir`` always points at the right place.
    The original Config object isn't mutated (it's frozen attrs); the
    caller stores the returned Config wherever the live one lives
    (typically ``app.openhost_config`` on the Quart app).
    """
    state = read_state(db)
    if state.backend == "s3":
        return config.evolve(archive_dir_override=juicefs_mount_dir(config))
    # Local backend: archive_dir_override should be unset so the
    # property falls back to persistent_data_dir/app_archive.
    return config.evolve(archive_dir_override=None)


def attach_on_startup(config: Config, db: sqlite3.Connection) -> Config:
    """Bring the archive backend back online after openhost-core boots.

    Returns a Config whose ``archive_dir_override`` matches the
    persisted backend so the caller can store it as the live config.
    For local backend: nothing to do; the directory is already there.
    For s3 backend: install JuiceFS if needed, then mount.

    Failures here MUST NOT crash the boot — we want the dashboard
    reachable even if the S3 backend is unhealthy, so the operator
    can fix it.  Surface the failure via the ``state_message`` column
    instead.

    On any failure we still return a Config matching the desired
    backend so that the api routes report the intended path.  Apps
    that try to deploy will fail loudly because the mount isn't
    actually up, which is the right semantic — silently falling
    back to local would let apps write to a path that gets shadowed
    when the operator fixes the S3 issue.
    """
    state = read_state(db)
    if state.state == "switching":
        # Boot in the middle of an in-flight switch.  We don't try
        # to resume — the operator's switch_backend caller already
        # left state_message in place; just clear the flag so the
        # dashboard isn't permanently locked.
        _update_state(
            db,
            state="idle",
            state_message=(state.state_message or "")
            + " (interrupted by openhost-core restart)",
        )
    if state.backend == "local":
        return apply_backend_to_config(config, db)
    if state.backend != "s3":
        logger.error("archive_backend has unknown backend %r", state.backend)
        return config
    try:
        if not is_juicefs_installed(config):
            install_juicefs(config)
        if state.s3_access_key_id is None or state.s3_secret_access_key is None:
            raise RuntimeError(
                "S3 credentials are missing from the archive_backend row; "
                "switch back to local from the dashboard and re-enter them."
            )
        mount(config, state.s3_access_key_id, state.s3_secret_access_key)
        _update_state(db, state="idle", state_message=None)
    except Exception as exc:
        logger.exception("Failed to attach archive backend on startup")
        _update_state(
            db,
            state="idle",
            state_message=f"Failed to attach archive backend: {exc}",
        )
    return apply_backend_to_config(config, db)


@attr.s(auto_attribs=True, frozen=True)
class MetaDumpSummary:
    """Summary of the JuiceFS metadata dumps living in ``<bucket>/<prefix>/meta/``.

    Surfaces JuiceFS's automatic hourly meta-backup in a form the
    dashboard can render without itself having to talk to S3 from JS.
    The bucket is the source of truth: counting and finding the
    most-recent dump means a single ListObjectsV2 pass.
    """

    count: int
    latest_at: str | None  # ISO 8601 string
    latest_key: str | None  # full S3 key, useful for diagnostics


def list_meta_dumps(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    s3_prefix: str | None,
) -> MetaDumpSummary | None:
    """Summarise the JuiceFS meta-dump objects in the bucket.

    Returns ``None`` on any failure (boto3 missing, bucket
    unreachable, list permission denied, etc.) — the dashboard
    treats ``None`` as "I can't see whether dumps exist" and renders
    accordingly.  Returning a structured error here would just push
    the rendering complexity into the route layer; ``None``-on-error
    keeps the GET path responsive even when S3 is having a bad day.

    Lists at most 1000 dumps in a single call (boto3's default page
    size).  An hourly cadence implies ~8760 dumps after a year, so
    we may eventually paginate, but pagination just to count for
    the dashboard's "N dumps in bucket" line is overkill — we cap
    the count at 1000 and label it as such if needed.
    """
    try:
        import boto3
    except ImportError:
        return None

    prefix = (s3_prefix or "").strip("/")
    list_prefix = f"{prefix}/meta/" if prefix else "meta/"
    try:
        kwargs: dict[str, object] = {
            "aws_access_key_id": s3_access_key_id,
            "aws_secret_access_key": s3_secret_access_key,
        }
        if s3_endpoint:
            kwargs["endpoint_url"] = s3_endpoint
        if s3_region:
            kwargs["region_name"] = s3_region
        client = boto3.client("s3", **kwargs)
        resp = client.list_objects_v2(
            Bucket=s3_bucket,
            Prefix=list_prefix,
            MaxKeys=1000,
        )
    except Exception:
        # Don't surface S3 errors as exceptions on the GET path.  The
        # operator-visible signal is "we don't know how recent the
        # last dump was" which the dashboard renders as a yellow
        # "Last metadata dump: unknown" line.
        logger.exception("list_meta_dumps: list_objects_v2 failed")
        return None

    contents = resp.get("Contents") or []
    # Filter to actual dump files (juicefs writes them as
    # ``meta/dump-YYYY-MM-DD-HHMMSS.json.gz``).  Defensive in case the
    # operator (or another tool) drops unrelated objects in the meta/
    # prefix; our count should reflect what JuiceFS itself wrote.
    dumps = [
        obj for obj in contents
        if obj.get("Key", "").rsplit("/", 1)[-1].startswith("dump-")
        and obj.get("Key", "").endswith(".json.gz")
    ]
    if not dumps:
        return MetaDumpSummary(count=0, latest_at=None, latest_key=None)

    latest = max(dumps, key=lambda obj: obj.get("LastModified") or 0)
    last_modified = latest.get("LastModified")
    latest_at: str | None = None
    if last_modified is not None:
        # boto3 returns timezone-aware datetimes.  Render as the
        # canonical ``2026-05-01T18:37:49Z`` shape we already use
        # elsewhere (see ``last_switched_at``).
        try:
            latest_at = last_modified.astimezone().strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            latest_at = str(last_modified)
    return MetaDumpSummary(
        count=len(dumps),
        latest_at=latest_at,
        latest_key=latest.get("Key"),
    )


def test_s3_credentials(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> str | None:
    """Try to reach the bucket with the given credentials.

    Returns ``None`` on success or a human-readable error string on
    failure.  Used by the dashboard's "Test connection" button before
    the operator commits to a backend switch.

    Uses boto3's ``head_bucket`` to validate.  ``boto3`` is imported
    lazily because openhost-core doesn't otherwise need it on every
    code path; if it's not installed, we return a descriptive error
    rather than running the check (an actual switch attempt would
    surface any cred problem via JuiceFS regardless).
    """
    try:
        import boto3
        import botocore.exceptions  # noqa: F401
    except ImportError:
        return (
            "boto3 is not installed in this openhost-core; cannot pre-flight "
            "the S3 connection.  The backend switch itself will surface "
            "any credential problems, but you'll have to roll back manually."
        )

    try:
        kwargs: dict[str, object] = {
            "aws_access_key_id": s3_access_key_id,
            "aws_secret_access_key": s3_secret_access_key,
        }
        if s3_endpoint:
            kwargs["endpoint_url"] = s3_endpoint
        if s3_region:
            kwargs["region_name"] = s3_region
        client = boto3.client("s3", **kwargs)
        client.head_bucket(Bucket=s3_bucket)
    except Exception as exc:
        return f"S3 reachability test failed: {exc}"
    return None


# ---------------------------------------------------------------------------
# Backend switch
# ---------------------------------------------------------------------------


# Stop-and-restart of opted-in apps is wired in via callbacks rather
# than direct imports of compute_space.core.containers / apps so this
# module stays unit-testable without standing up the whole web stack.
# The api layer wires the real callbacks; tests pass fakes.

@attr.s(auto_attribs=True, frozen=True)
class AppHook:
    """Callbacks the api layer hands to ``switch_backend`` so the
    archive-backend code stays decoupled from the apps/containers
    modules that import each other heavily.

    ``set_config`` is called after the backend swap with a new
    ``Config`` whose ``archive_dir_override`` matches the new state.
    The api layer wires this to ``app.openhost_config = new_config``
    so subsequent requests see the new path.
    """

    list_app_archive_apps: Callable[[], list[str]]
    stop_app: Callable[[str], None]
    start_app: Callable[[str], None]
    set_config: Callable[[Config], None]


class BackendSwitchError(Exception):
    """Raised by ``switch_backend`` when a step in the flow fails.

    The DB ``state_message`` is also populated so the dashboard can
    show the operator what went wrong; raising the exception lets
    the api layer return a 500 with the same string.
    """


def _copy_tree(src: str, dst: str) -> None:
    """Recursively copy every entry under ``src`` into ``dst``.  Used by
    the migrate phase of a backend switch.

    Symlinks are recreated as symlinks (not followed) so we don't
    expand a symlink-to-a-large-dir into N copies of the data and
    so that inter-app references the operator may have set up
    survive the switch.  Sockets, FIFOs, and devices that the
    operator inexplicably stuck under app_archive are skipped with
    a warning rather than aborting the whole switch.
    """
    os.makedirs(dst, exist_ok=True)
    for entry in os.scandir(src):
        s = entry.path
        d = os.path.join(dst, entry.name)
        if entry.is_symlink():
            # Recreate the symlink at the destination.  ``os.readlink``
            # returns the target verbatim — we don't try to rewrite
            # paths because the source and destination archive trees
            # have the same per-app subdir layout, so relative links
            # stay valid.  An absolute link would point at the same
            # place either way.
            target = os.readlink(s)
            try:
                if os.path.islink(d) or (os.path.lexists(d) and not os.path.isdir(d)):
                    # File or symlink at the destination — replace it.
                    os.unlink(d)
                elif os.path.isdir(d) and not os.path.islink(d):
                    # Real directory at the destination — rmtree to
                    # make way for the symlink.  ``os.unlink`` would
                    # IsADirectoryError here and the symlink would be
                    # silently lost.
                    shutil.rmtree(d)
                os.symlink(target, d)
            except OSError as exc:
                logger.warning("Failed to recreate symlink %s -> %s: %s", d, target, exc)
        elif entry.is_dir(follow_symlinks=False):
            _copy_tree(s, d)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(s, d)
        else:
            logger.warning(
                "Skipping non-regular entry %s during archive backend switch", s
            )


@attr.s(auto_attribs=True, frozen=True)
class _SwitchPlan:
    """Internal plan derived from operator inputs + DB state.

    Computed once at the top of ``switch_backend`` and passed to the
    extracted helper phases so each phase reads what it needs without
    rederiving it.
    """

    current: BackendState
    target_backend: str
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    s3_prefix: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    volume_name: str
    delete_source_after_copy: bool


def _bring_up_target(config: Config, db: sqlite3.Connection, plan: _SwitchPlan) -> tuple[str, bool]:
    """Install + format + mount the new backend; return (new_archive_dir, mount_active).

    ``mount_active`` is True iff this call brought a new JuiceFS
    mount up — used by the failure path to umount it again.
    """
    if plan.target_backend == "s3":
        _update_state(db, state_message="Installing juicefs")
        install_juicefs(config)
        _update_state(db, state_message="Formatting volume")
        format_volume(
            config,
            s3_bucket=plan.s3_bucket,
            s3_region=plan.s3_region,
            s3_endpoint=plan.s3_endpoint,
            s3_access_key_id=plan.s3_access_key_id,
            s3_secret_access_key=plan.s3_secret_access_key,
            juicefs_volume_name=plan.volume_name,
        )
        _update_state(db, state_message="Mounting volume")
        mount(config, plan.s3_access_key_id, plan.s3_secret_access_key)
        return juicefs_mount_dir(config), True
    new_archive_dir = os.path.join(config.persistent_data_dir, "app_archive")
    os.makedirs(new_archive_dir, exist_ok=True)
    return new_archive_dir, False


def _migrate_archive_data(
    db: sqlite3.Connection,
    plan: _SwitchPlan,
    old_archive_dir: str,
    new_archive_dir: str,
) -> None:
    """Wipe stale entries in the destination, then copy source -> destination.

    The wipe ensures a switch from an empty source doesn't silently
    leave whatever was there from a previous switch.

    For the s3 -> local direction, we additionally require that the
    source JuiceFS mount is actually live before copying — otherwise
    we'd wipe the destination and copy from an effectively empty
    source, silently dropping every byte the operator had on S3.
    """
    if plan.current.backend == "s3" and not is_mounted(old_archive_dir):
        raise BackendSwitchError(
            f"Source JuiceFS mount at {old_archive_dir!r} is not live; "
            f"refusing to copy from it because the result would be a "
            f"silent data loss (we'd copy from an empty mount-point and "
            f"wipe the destination).  Investigate the mount status and "
            f"retry the switch."
        )
    _update_state(db, state_message="Copying archive data")
    # Wipe the destination unconditionally before copy.  Earlier
    # versions of this code skipped the wipe on local->s3 on the
    # assumption that a freshly-formatted JuiceFS volume was always
    # empty.  ``format_volume`` is non-destructive on an existing
    # volume though, so a local->s3->local->s3 cycle would leave
    # stale per-app dirs in the JuiceFS bucket on the second
    # local->s3.  Wipe to keep the destination consistent with the
    # source after the copy.
    if os.path.isdir(new_archive_dir):
        for entry in list(os.scandir(new_archive_dir)):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
            except OSError as exc:
                # Log but don't abort — the new copy will overlay
                # any partially-removed dir, and the operator gets a
                # visible warning rather than a silent retain.
                logger.warning(
                    "Failed to remove stale entry %s before copy: %s",
                    entry.path,
                    exc,
                )
    if os.path.isdir(old_archive_dir):
        _copy_tree(old_archive_dir, new_archive_dir)


def _tear_down_source(
    config: Config, db: sqlite3.Connection, plan: _SwitchPlan, old_archive_dir: str
) -> str | None:
    """Umount the old s3 mount (if any) and optionally delete source-side data.

    Returns a non-fatal warning string if delete_source_after_copy
    was requested but the rmtree failed; ``switch_backend`` surfaces
    that via ``state_message`` so the operator's dashboard sees the
    'switch succeeded but old data wasn't actually freed' case
    instead of a green checkmark.

    A failed umount is fatal: leaving the JuiceFS mount up while the
    DB says backend=local would orphan the FUSE process; worse, if
    delete_source_after_copy is set we'd be about to ``rmtree`` the
    still-mounted path and JuiceFS would obediently delete every chunk
    in S3.
    """
    if plan.current.backend == "s3":
        _update_state(db, state_message="Unmounting old volume")
        try:
            umount(config)
        except Exception as exc:
            raise BackendSwitchError(
                f"Failed to unmount the old S3 backend — refusing to "
                f"continue because the next steps could destroy live "
                f"data.  Original error: {exc}"
            ) from exc

    # Optionally delete source-side data once the copy made it to the
    # new home AND any source mount is torn down.  Only frees space on
    # the LOCAL backend; on s3->local the rmtree here removes the
    # empty FUSE mount-point dir on local disk but doesn't touch S3.
    warning: str | None = None
    if plan.delete_source_after_copy and os.path.isdir(old_archive_dir):
        try:
            shutil.rmtree(old_archive_dir)
        except OSError as exc:
            logger.warning(
                "delete_source_after_copy: rmtree(%s) failed: %s",
                old_archive_dir,
                exc,
            )
            warning = (
                f"switch succeeded, but delete_source_after_copy failed "
                f"to remove {old_archive_dir!r}: {exc}.  Operator may "
                f"need to remove it manually to reclaim disk space."
            )
        else:
            # Recreate the empty local default so future deploys that
            # happen to run before another switch don't fail.
            if plan.current.backend == "local":
                os.makedirs(old_archive_dir, exist_ok=True)
    return warning


def switch_backend(
    config: Config,
    db: sqlite3.Connection,
    hook: AppHook,
    *,
    target_backend: str,
    s3_bucket: str | None = None,
    s3_region: str | None = None,
    s3_endpoint: str | None = None,
    s3_prefix: str | None = None,
    s3_access_key_id: str | None = None,
    s3_secret_access_key: str | None = None,
    juicefs_volume_name: str | None = None,
    delete_source_after_copy: bool = False,
) -> None:
    """Switch the archive backend, copying data + restarting opted-in
    apps as needed.

    The high-level steps are:

    1. Persist ``state='switching'`` so a crash mid-flow leaves
       enough breadcrumbs in the DB for the operator to see what
       happened on the next dashboard load.
    2. Stop every running app that opted into ``app_archive`` (or
       ``access_all_data``).  This is destructive from the app's
       perspective but the dashboard already warned the operator.
    3. Bring up the target backend (install + format + mount JuiceFS
       if going to s3; nothing if going to local).
    4. Copy data from the source backend's host-side path into the
       target backend's host-side path.
    5. Tear down the source backend if any (umount JuiceFS if
       going local).
    6. Persist the new backend state and clear ``state``.
    7. Restart the apps from step 2.

    On any failure between steps 3-6, the function tries to leave the
    system in a recoverable state: the source backend is not torn
    down until the copy succeeds, so a failed switch can be retried
    or rolled back manually.
    """
    if target_backend not in ("local", "s3"):
        raise BackendSwitchError(f"Unknown target backend {target_backend!r}")

    if target_backend == "s3":
        if not (s3_bucket and s3_access_key_id and s3_secret_access_key):
            raise BackendSwitchError(
                "Switching to s3 requires bucket, access_key_id, and "
                "secret_access_key."
            )

    # Atomically claim the switching slot.  Two concurrent POSTs
    # both passing the read_state check would otherwise enter the
    # flow side-by-side and step on each other — overlapping
    # stops/copies/mounts/unmounts.  The single-row UPDATE-WHERE
    # only succeeds for one caller; the loser raises.  rowcount==0
    # means somebody else got it (or the row is missing, which
    # shouldn't happen because the v5 migration seeds it).
    cur = db.execute(
        "UPDATE archive_backend SET state='switching', state_message='Starting' "
        "WHERE id=1 AND state='idle'"
    )
    db.commit()
    if cur.rowcount == 0:
        raise BackendSwitchError(
            "Archive backend is already in state 'switching'; "
            "wait for the in-flight switch to finish before starting a new one."
        )

    # ``stopped_apps`` is the set we successfully stopped (and
    # therefore must restart in the finally block).  ``affected_apps``
    # is the candidate list — used only to decide what to try to
    # stop.  Splitting them avoids the failure mode where a stop
    # raised partway through the loop and the finally tried to start
    # apps that were never stopped, producing spurious 'starting'
    # transitions on apps that were already healthy.
    affected_apps: list[str] = []
    stopped_apps: list[str] = []
    new_mount_active = False  # set when the s3 target is up; used by
    # the failure path to umount it again so we don't orphan a
    # FUSE process while the DB rolls back to the old backend.
    try:
        # ``read_state`` and the no-op short-circuit live INSIDE the
        # try/except so a transient sqlite read failure doesn't leave
        # the row permanently stuck in 'switching' — the finally
        # clause and the except handlers will release the lock.
        current = read_state(db)
        # current.state is now 'switching' but its other fields
        # (backend, creds, etc.) reflect the pre-switch state, which
        # is what we want.
        current = attr.evolve(current, state="idle")

        if target_backend == current.backend:
            # No-op.  Release the lock and return.  This keeps the
            # dashboard's idempotent re-saves from surprising failures.
            _update_state(db, state="idle", state_message=None)
            return

        # The operator-supplied ``s3_prefix`` doubles as the JuiceFS
        # volume name.  JuiceFS's S3 backend cannot store data under
        # an arbitrary path inside a bucket — it insists on parsing
        # the bucket URL itself as the bucket name and uses the
        # volume name (the trailing positional arg of ``juicefs
        # format``) as the prefix for every object it writes.  So
        # the cleanest mapping is: prefix => volume_name.  When no
        # prefix is set, fall back to the explicit volume_name form
        # field, then the previously-recorded volume name, then
        # "openhost" as the safe default.
        if target_backend == "s3":
            volume_name = (
                s3_prefix
                or juicefs_volume_name
                or current.juicefs_volume_name
                or "openhost"
            )
        else:
            volume_name = current.juicefs_volume_name or "openhost"

        _update_state(db, state_message="Stopping apps")

        # Stop every running ``app_archive`` app.  We catch the list
        # while apps are still running so a failed switch can restart
        # exactly the same set.  ``stop_app`` failures are fatal:
        # if an app isn't actually stopped, it's still writing to the
        # source archive while we try to copy from it, violating
        # the copy's consistency guarantee.  Better to abort.
        affected_apps = list(hook.list_app_archive_apps())
        for name in affected_apps:
            try:
                hook.stop_app(name)
            except Exception as exc:
                raise BackendSwitchError(
                    f"Failed to stop {name} for backend switch — refusing to "
                    f"continue because in-flight writes from a still-running "
                    f"app would corrupt the data copy.  Original error: {exc}"
                ) from exc
            stopped_apps.append(name)

        old_archive_dir = archive_dir_for_backend(config, current.backend)
        plan = _SwitchPlan(
            current=current,
            target_backend=target_backend,
            s3_bucket=s3_bucket,
            s3_region=s3_region,
            s3_endpoint=s3_endpoint,
            s3_prefix=s3_prefix,
            s3_access_key_id=s3_access_key_id,
            s3_secret_access_key=s3_secret_access_key,
            volume_name=volume_name,
            delete_source_after_copy=delete_source_after_copy,
        )
        new_archive_dir, new_mount_active = _bring_up_target(config, db, plan)
        _migrate_archive_data(db, plan, old_archive_dir, new_archive_dir)
        teardown_warning = _tear_down_source(config, db, plan, old_archive_dir)

        # Persist the new state.  We bypass _update_state for the s3
        # fields here because we need to write them to the EXACT
        # values the operator submitted — including None for region/
        # endpoint when they're switching to a new bucket without
        # specifying those.  _update_state's "None means skip" rule
        # would otherwise let stale region/endpoint values from a
        # previous s3 switch silently persist and route the next
        # mount to the wrong AWS endpoint.
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if target_backend == "s3":
            db.execute(
                "UPDATE archive_backend SET state='idle', state_message=?, "
                "backend='s3', s3_bucket=?, s3_region=?, s3_endpoint=?, "
                "s3_prefix=?, s3_access_key_id=?, s3_secret_access_key=?, "
                "juicefs_volume_name=?, last_switched_at=? WHERE id=1",
                (
                    teardown_warning,
                    s3_bucket,
                    s3_region,
                    s3_endpoint,
                    s3_prefix,
                    s3_access_key_id,
                    s3_secret_access_key,
                    volume_name,
                    now_iso,
                ),
            )
        else:
            # Switching to local: clear creds (sensitive — drop) but
            # keep bucket/region/endpoint so the operator's next switch
            # back to s3 form is pre-filled with their last config.
            db.execute(
                "UPDATE archive_backend SET state='idle', state_message=?, "
                "backend='local', s3_access_key_id=NULL, "
                "s3_secret_access_key=NULL, juicefs_volume_name=?, "
                "last_switched_at=? WHERE id=1",
                (
                    teardown_warning,
                    volume_name,
                    now_iso,
                ),
            )
        db.commit()

        # Hand the api layer a Config whose archive_dir_override now
        # matches the new backend.  Apps started below see the new
        # path; existing references to the old Config are stale but
        # the route layer always re-fetches via ``get_config()``.
        hook.set_config(apply_backend_to_config(config, db))
        new_mount_active = False  # state now matches the live mount
    except Exception as exc:
        _update_state(db, state="idle", state_message=f"switch failed: {exc}")
        if new_mount_active:
            try:
                umount(config)
            except Exception:
                logger.exception(
                    "Failed to umount JuiceFS during switch rollback; the "
                    "FUSE process is orphaned and the operator may need to "
                    "umount it manually."
                )
        # Wrap non-BackendSwitchError failures so the api layer always
        # gets the same exception type.  Pre-existing BackendSwitchError
        # passes through (its __cause__ + message are already shaped
        # for the operator).
        if isinstance(exc, BackendSwitchError):
            raise
        raise BackendSwitchError(str(exc)) from exc
    finally:
        # Always restart the apps we successfully stopped, success or
        # failure.  Without this, a failed switch leaves them in
        # ``stopped`` forever — operators retrying the switch would
        # find the affected_apps list empty (because the apps are no
        # longer running) and the apps would be permanently orphaned.
        # On a failed switch, restarts may themselves fail (e.g. if
        # the new backend is broken); those failures surface as DB
        # ``error_message`` per-app, which is the right operator-
        # visible signal.  Only ``stopped_apps`` is iterated, NOT
        # ``affected_apps``, so we don't try to start an app whose
        # earlier stop_app raised (it was never stopped).
        for name in stopped_apps:
            try:
                hook.start_app(name)
            except Exception:
                logger.exception("Failed to restart %s after backend switch", name)
