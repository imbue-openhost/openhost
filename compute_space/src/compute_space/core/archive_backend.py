"""Operator-controlled archive backend management.  See
``docs/data.md`` for the operator-facing model."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import attr
import boto3
import botocore.exceptions  # noqa: F401  -- imported for ``except`` matching downstream

from compute_space.config import Config
from compute_space.core.logging import logger

# Pin a specific JuiceFS release; sha256 is verified before extract so a
# compromised release page can't swap the tarball.
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


def _juicefs_state_dir(config: Config) -> str:
    """Critical state that must survive reboots (meta.db); back this up."""
    return os.path.join(config.openhost_data_path, "juicefs", "state")


def _juicefs_runtime_dir(config: Config) -> str:
    """Regenerable state (binary, etc.); safe to wipe."""
    return os.path.join(config.openhost_data_path, "juicefs", "runtime")


def _juicefs_install_dir(config: Config) -> str:
    return os.path.join(_juicefs_runtime_dir(config), "bin")


def _juicefs_binary(config: Config) -> str:
    return os.path.join(_juicefs_install_dir(config), f"juicefs-{JUICEFS_VERSION}")


def _juicefs_meta_db(config: Config) -> str:
    return os.path.join(_juicefs_state_dir(config), "meta.db")


_LEGACY_META_DB_BASENAME = "juicefs-meta.db"
_LEGACY_INSTALL_DIRNAME = "juicefs"


def _migrate_legacy_layout(config: Config) -> None:
    """One-shot migration of pre-tidy paths into juicefs/state + juicefs/runtime.  Idempotent."""
    legacy_meta = os.path.join(config.openhost_data_path, _LEGACY_META_DB_BASENAME)
    new_meta = _juicefs_meta_db(config)
    legacy_install = os.path.join(config.openhost_data_path, _LEGACY_INSTALL_DIRNAME)

    # The legacy install dir shares a name with the new juicefs/ parent of
    # state/ + runtime/, so disambiguate by looking for binaries at its top
    # level (vs. nested under runtime/bin/).
    if os.path.isdir(legacy_install):
        legacy_top_level_files = []
        try:
            for entry in os.scandir(legacy_install):
                if entry.is_file() and entry.name.startswith("juicefs-"):
                    legacy_top_level_files.append(entry.path)
        except OSError:
            legacy_top_level_files = []
        if legacy_top_level_files:
            new_bin_dir = _juicefs_install_dir(config)
            os.makedirs(new_bin_dir, exist_ok=True)
            for src in legacy_top_level_files:
                dst = os.path.join(new_bin_dir, os.path.basename(src))
                if not os.path.exists(dst):
                    try:
                        os.rename(src, dst)
                        logger.info("Migrated legacy juicefs binary %s -> %s", src, dst)
                    except OSError as exc:
                        # Non-fatal: install_juicefs redownloads if needed.
                        logger.warning(
                            "Could not migrate legacy juicefs binary %s -> %s: %s",
                            src,
                            dst,
                            exc,
                        )

    if os.path.isfile(legacy_meta) and not os.path.exists(new_meta):
        os.makedirs(os.path.dirname(new_meta), exist_ok=True)
        try:
            os.rename(legacy_meta, new_meta)
            logger.info(
                "Migrated JuiceFS metadata DB %s -> %s (one-shot layout tidy)",
                legacy_meta,
                new_meta,
            )
        except OSError as exc:
            # Loss of meta.db makes the bucket unreadable; never let a second
            # copy mysteriously appear at the new path.
            raise RuntimeError(
                f"Could not migrate JuiceFS metadata DB from {legacy_meta!r} "
                f"to {new_meta!r}: {exc}.  Fix the rename manually before retrying."
            ) from exc


def juicefs_mount_dir(config: Config) -> str:
    """The host-side JuiceFS FUSE mount; bind-mounted into containers."""
    # Under data_root_dir, NOT persistent_data_dir, so restic backups don't
    # double-store bytes that already live in S3.
    return os.path.join(config.data_root_dir, "app_archive")


def juicefs_meta_db_path(config: Config) -> str:
    return _juicefs_meta_db(config)


def juicefs_state_dir(config: Config) -> str:
    return _juicefs_state_dir(config)


def is_juicefs_installed(config: Config) -> bool:
    return os.path.isfile(_juicefs_binary(config)) and os.access(_juicefs_binary(config), os.X_OK)


def install_juicefs(config: Config) -> None:
    """Download + verify + extract the JuiceFS binary.  Idempotent."""
    _migrate_legacy_layout(config)
    if is_juicefs_installed(config):
        return
    install_dir = _juicefs_install_dir(config)
    os.makedirs(install_dir, exist_ok=True)
    arch = _arch()
    expected_sha = JUICEFS_SHA256.get(arch)
    if not expected_sha:
        raise RuntimeError(f"No pinned JuiceFS sha256 for arch {arch!r}; refusing to install.")
    url = (
        f"https://github.com/juicedata/juicefs/releases/download/"
        f"v{JUICEFS_VERSION}/juicefs-{JUICEFS_VERSION}-linux-{arch}.tar.gz"
    )
    logger.info("Downloading JuiceFS %s for %s", JUICEFS_VERSION, arch)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            tarball_bytes = resp.read()
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Failed to download JuiceFS: {exc}") from exc

    actual_sha = hashlib.sha256(tarball_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"JuiceFS tarball sha256 mismatch (expected {expected_sha}, got {actual_sha}).  Refusing to install."
        )

    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == "juicefs"), None)
        if member is None:
            raise RuntimeError("JuiceFS tarball missing the ``juicefs`` binary")
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
    """JuiceFS bucket URL.  Do NOT append a path component: JuiceFS's S3
    backend parses the first path segment as the bucket name (pkg/object/s3.go),
    so any extra path here would break the DNS lookup.  Per-zone isolation is
    handled via the volume name prefix instead.
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

    ``juicefs_volume_name`` doubles as the per-zone object prefix (every
    chunk lands under ``<bucket>/<volume>/...``), so two zones can share
    one bucket safely.
    """
    bucket_url = _bucket_url(s3_bucket, s3_region or "us-east-1", s3_endpoint)
    cmd = [
        _juicefs_binary(config),
        # --no-agent: skip JuiceFS's pprof HTTP agent (binds 6060..6099) so the
        # security audit doesn't flag a transient unexpected listener.
        "--no-agent",
        "format",
        "--storage",
        "s3",
        "--bucket",
        bucket_url,
        _format_meta_dsn(config),
        juicefs_volume_name,
    ]
    # Pass S3 creds via env, not argv, so they don't leak into ``ps``.
    env = os.environ.copy()
    env["ACCESS_KEY"] = s3_access_key_id
    env["SECRET_KEY"] = s3_secret_access_key
    logger.info("Running juicefs format against %s", bucket_url)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"juicefs format failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )


_mount_lock = threading.Lock()
_mount_proc: subprocess.Popen[bytes] | None = None


def is_mounted(mount_point: str) -> bool:
    """Return True iff ``mount_point`` is a live mount.

    Uses /proc/self/mountinfo because ``os.path.ismount`` breaks on some
    FS/userns combinations.
    """
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
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
    """Start ``juicefs mount`` as a supervised child process.  Idempotent."""
    global _mount_proc
    mount_point = juicefs_mount_dir(config)
    os.makedirs(mount_point, exist_ok=True)

    with _mount_lock:
        if is_mounted(mount_point):
            logger.info("juicefs already mounted at %s", mount_point)
            return
        env = os.environ.copy()
        # Creds via env, not argv, to keep them out of ``ps``.
        env["ACCESS_KEY"] = s3_access_key_id
        env["SECRET_KEY"] = s3_secret_access_key
        cmd = [
            _juicefs_binary(config),
            # --no-agent: mount spawns two processes that each bind 6060/6061
            # for pprof, which the security audit flags as unexpected.
            "--no-agent",
            "mount",
            "--no-usage-report",
            _format_meta_dsn(config),
            mount_point,
        ]
        logger.info("Starting juicefs mount at %s", mount_point)
        # DEVNULL stdout/stderr: a long-lived mount filling a 64 KiB pipe
        # buffer would freeze; juicefs has its own log file anyway.
        _mount_proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if is_mounted(mount_point):
                logger.info("juicefs mount ready at %s", mount_point)
                return
            rc = _mount_proc.poll()
            if rc is not None:
                _mount_proc = None
                raise RuntimeError(f"juicefs mount exited early (rc={rc}); check ~/.juicefs/juicefs.log")
            time.sleep(0.2)
        # Kill the stuck child so it doesn't hold the mount-point lock and
        # block retries.
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
        raise RuntimeError(f"juicefs mount did not become ready within 15s at {mount_point}")


def umount(config: Config) -> None:
    """Unmount the JuiceFS mount and reap the supervised process.

    Surfaces a busy-FS failure rather than swallowing it; we deliberately
    don't have root, so lazy unmount isn't an option.  Idempotent.
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
                        logger.error("juicefs mount process did not exit after SIGKILL")
            _mount_proc = None
            return
        cmd = [_juicefs_binary(config), "umount", mount_point]
        # Always clear _mount_proc on any exit path so a retry doesn't
        # inherit a stale handle.
        try:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"juicefs umount of {mount_point} timed out after 30s") from exc
            if result.returncode != 0:
                raise RuntimeError(
                    f"juicefs umount of {mount_point} failed "
                    f"(rc={result.returncode}); ensure all containers "
                    f"using the archive tier are stopped before switching "
                    f"backends.  Original: {result.stderr.strip()}"
                )
            logger.info("juicefs unmounted from %s", mount_point)
        finally:
            if _mount_proc is not None:
                try:
                    _mount_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _mount_proc.kill()
                    try:
                        _mount_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.error("juicefs mount process did not exit after SIGKILL")
            _mount_proc = None


@attr.s(auto_attribs=True, frozen=True)
class BackendState:
    """Operator-visible archive backend state."""

    backend: str  # "disabled" | "local" | "s3"
    state: str  # "idle" | "switching"
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
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
        # Defensive fallback for a partial DB; migrations seed this row.
        return BackendState(
            backend="disabled",
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

    ``None`` means "don't update this field".  Two deliberate exceptions:
    passing ``state`` always rewrites ``state_message`` (clearing stale
    text on transition), and ``clear_s3_credentials=True`` NULLs the
    access key columns.
    """
    fields: dict[str, object | None] = {}
    if state is not None:
        fields["state"] = state
    if state_message is not None or state is not None:
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
        # Drop creds (sensitive); keep bucket/region/endpoint so the
        # operator's next switch-back-to-s3 form pre-fills.
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


# Anchored on TOML key=value shape so substring matching can't false-match
# ``app_archive = false`` alongside an unrelated ``= true``.
_MANIFEST_REQUIRES_ARCHIVE_RE = re.compile(r"(?m)^\s*app_archive\s*=\s*[Tt][Rr][Uu][Ee]\b")
_MANIFEST_USES_ARCHIVE_RE = re.compile(r"(?m)^\s*(?:app_archive|access_all_data)\s*=\s*[Tt][Rr][Uu][Ee]\b")


def manifest_requires_archive(manifest_raw: str) -> bool:
    """Return True iff the manifest cannot run without the archive tier
    (``app_archive = true``).  ``access_all_data`` does NOT qualify."""
    return bool(_MANIFEST_REQUIRES_ARCHIVE_RE.search(manifest_raw))


def manifest_uses_archive(manifest_raw: str) -> bool:
    """Return True iff the app gets archive-mount access when deployed
    (``app_archive = true`` OR ``access_all_data = true``)."""
    return bool(_MANIFEST_USES_ARCHIVE_RE.search(manifest_raw))


def is_archive_dir_healthy(config: Config, db: sqlite3.Connection) -> bool:
    """Return True iff the configured archive backing is live on the host.

    For s3 we MUST check ``is_mounted``, not ``os.path.isdir``: an empty
    mount-point dir would silently let writes fall through to local disk
    where they'd be shadowed when JuiceFS reattaches.
    """
    state = read_state(db)
    if state.backend == "disabled":
        return False
    if state.backend == "s3":
        return is_mounted(juicefs_mount_dir(config))
    return os.path.isdir(os.path.join(config.persistent_data_dir, "app_archive"))


def archive_dir_for_backend(config: Config, backend: str) -> str | None:
    """Return the host-side archive root for ``backend``, or None for
    ``disabled`` (no backing)."""
    if backend == "s3":
        return juicefs_mount_dir(config)
    if backend == "local":
        return os.path.join(config.persistent_data_dir, "app_archive")
    return None


def apply_backend_to_config(config: Config, db: sqlite3.Connection) -> Config:
    """Return a Config whose ``archive_dir_override`` matches the persisted backend."""
    state = read_state(db)
    if state.backend == "s3":
        return config.evolve(archive_dir_override=juicefs_mount_dir(config))
    return config.evolve(archive_dir_override=None)


def attach_on_startup(config: Config, db: sqlite3.Connection) -> Config:
    """Bring the archive backend back online at boot.

    Failures must NOT crash the boot — they're surfaced via
    ``state_message`` so the dashboard stays reachable.  Returns a Config
    matching the desired backend even on failure; silently falling back
    to local would let apps write to a path that gets shadowed when the
    operator fixes the S3 issue.
    """
    state = read_state(db)
    if state.state == "switching":
        # Booted mid-switch; don't try to resume, just unstick the dashboard.
        _update_state(
            db,
            state="idle",
            state_message=(state.state_message or "") + " (interrupted by openhost-core restart)",
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


def _s3_client(
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> Any:
    """Build a boto3 S3 client; only passes region/endpoint when set so
    boto3 default-region behaviour is preserved."""
    kwargs: dict[str, object] = {
        "aws_access_key_id": s3_access_key_id,
        "aws_secret_access_key": s3_secret_access_key,
    }
    if s3_endpoint:
        kwargs["endpoint_url"] = s3_endpoint
    if s3_region:
        kwargs["region_name"] = s3_region
    return boto3.client("s3", **kwargs)


@attr.s(auto_attribs=True, frozen=True)
class MetaDumpSummary:
    """Summary of JuiceFS metadata dumps under ``<bucket>/<prefix>/meta/``."""

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
    """Summarise JuiceFS meta-dump objects.  Returns None on any error
    (the dashboard renders that as "unknown").  Caps at 1000 dumps."""
    prefix = (s3_prefix or "").strip("/")
    list_prefix = f"{prefix}/meta/" if prefix else "meta/"
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        resp = client.list_objects_v2(
            Bucket=s3_bucket,
            Prefix=list_prefix,
            MaxKeys=1000,
        )
    except Exception:
        logger.exception("list_meta_dumps: list_objects_v2 failed")
        return None

    contents = resp.get("Contents") or []
    # Filter to JuiceFS's own dump filenames so unrelated objects under
    # meta/ don't inflate the count.
    dumps = [
        obj
        for obj in contents
        if obj.get("Key", "").rsplit("/", 1)[-1].startswith("dump-") and obj.get("Key", "").endswith(".json.gz")
    ]
    if not dumps:
        return MetaDumpSummary(count=0, latest_at=None, latest_key=None)

    latest = max(dumps, key=lambda obj: obj.get("LastModified") or 0)
    last_modified = latest.get("LastModified")
    latest_at: str | None = None
    if last_modified is not None:
        try:
            latest_at = last_modified.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
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
    """Probe the bucket with ``head_bucket``.  Returns None on success
    or a human-readable error string."""
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        client.head_bucket(Bucket=s3_bucket)
    except Exception as exc:
        return f"S3 reachability test failed: {exc}"
    return None


@attr.s(auto_attribs=True, frozen=True)
class AppHook:
    """Callbacks ``switch_backend`` uses to drive the apps/containers layer
    without importing it directly (keeps this module unit-testable)."""

    list_app_archive_apps: Callable[[], list[str]]
    stop_app: Callable[[str], None]
    start_app: Callable[[str], None]
    set_config: Callable[[Config], None]


class BackendSwitchError(Exception):
    """Raised by ``switch_backend`` when a step in the flow fails."""


def _copy_tree(src: str, dst: str) -> None:
    """Recursive copy preserving symlinks (not followed) so a symlink
    to a large dir doesn't explode into N copies."""
    os.makedirs(dst, exist_ok=True)
    for entry in os.scandir(src):
        s = entry.path
        d = os.path.join(dst, entry.name)
        if entry.is_symlink():
            target = os.readlink(s)
            try:
                if os.path.islink(d) or (os.path.lexists(d) and not os.path.isdir(d)):
                    os.unlink(d)
                elif os.path.isdir(d) and not os.path.islink(d):
                    # rmtree because os.unlink on a real dir would
                    # IsADirectoryError and the symlink would be lost.
                    shutil.rmtree(d)
                os.symlink(target, d)
            except OSError as exc:
                logger.warning("Failed to recreate symlink %s -> %s: %s", d, target, exc)
        elif entry.is_dir(follow_symlinks=False):
            _copy_tree(s, d)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(s, d)
        else:
            logger.warning("Skipping non-regular entry %s during archive backend switch", s)


@attr.s(auto_attribs=True, frozen=True)
class _SwitchPlan:
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
    """Install + format + mount the new backend; return (new_archive_dir, mount_active)."""
    if plan.target_backend == "s3":
        # Already validated upstream; asserts narrow for mypy.
        assert plan.s3_bucket is not None, "s3 target requires bucket"
        assert plan.s3_access_key_id is not None, "s3 target requires access_key_id"
        assert plan.s3_secret_access_key is not None, "s3 target requires secret_access_key"
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
    """Wipe destination, then copy source -> destination."""
    # Refuse to copy from a not-actually-mounted s3 source; otherwise we'd
    # wipe the destination and copy from an empty mount-point dir, silently
    # dropping every byte the operator had in S3.
    if plan.current.backend == "s3" and not is_mounted(old_archive_dir):
        raise BackendSwitchError(
            f"Source JuiceFS mount at {old_archive_dir!r} is not live; "
            f"refusing to copy from it because the result would be a "
            f"silent data loss.  Investigate the mount status and retry."
        )
    _update_state(db, state_message="Copying archive data")
    # Wipe unconditionally: format_volume is non-destructive on existing
    # volumes, so a local->s3->local->s3 cycle would otherwise leave
    # stale per-app dirs in the bucket.
    if os.path.isdir(new_archive_dir):
        for entry in list(os.scandir(new_archive_dir)):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove stale entry %s before copy: %s",
                    entry.path,
                    exc,
                )
    if os.path.isdir(old_archive_dir):
        _copy_tree(old_archive_dir, new_archive_dir)


def _tear_down_source(config: Config, db: sqlite3.Connection, plan: _SwitchPlan, old_archive_dir: str) -> str | None:
    """Umount the old s3 mount (if any) and optionally delete source data.

    A failed umount must be fatal: if delete_source_after_copy is set
    we'd ``rmtree`` a still-mounted path and JuiceFS would obediently
    delete every chunk in S3.
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
            # Recreate the empty local default so deploys before the next
            # switch don't fail on a missing dir.
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
    """Switch the archive backend: stop opted-in apps, bring up the
    target, copy data, tear down the source, persist state, restart apps.

    Source teardown only runs after the copy succeeds, so a failed
    switch is retryable / recoverable.
    """
    if target_backend not in ("disabled", "local", "s3"):
        raise BackendSwitchError(f"Unknown target backend {target_backend!r}")

    if target_backend == "s3":
        if not (s3_bucket and s3_access_key_id and s3_secret_access_key):
            raise BackendSwitchError("Switching to s3 requires bucket, access_key_id, and secret_access_key.")

    # Atomically claim the switching slot so concurrent POSTs can't both
    # enter the flow and overlap stops/copies/mounts/unmounts.
    cur = db.execute(
        "UPDATE archive_backend SET state='switching', state_message='Starting' WHERE id=1 AND state='idle'"
    )
    db.commit()
    if cur.rowcount == 0:
        raise BackendSwitchError(
            "Archive backend is already in state 'switching'; "
            "wait for the in-flight switch to finish before starting a new one."
        )

    # stopped_apps != affected_apps: only restart apps we actually stopped,
    # so a failed stop_app mid-loop doesn't trigger spurious starts on apps
    # that were already healthy.
    affected_apps: list[str] = []
    stopped_apps: list[str] = []
    # new_mount_active: failure path uses this to umount the just-brought-up
    # s3 mount so we don't orphan a FUSE process when the DB rolls back.
    new_mount_active = False
    try:
        # Read state INSIDE the try so a sqlite hiccup doesn't permanently
        # strand the row in 'switching'.
        current = read_state(db)
        current = attr.evolve(current, state="idle")

        if target_backend == current.backend:
            _update_state(db, state="idle", state_message=None)
            return

        # disabled is reserved for fresh zones; stepping down to it from a
        # configured backend would orphan archive bytes with no openhost
        # handle to reach them.
        if target_backend == "disabled" and current.backend != "disabled":
            raise BackendSwitchError(
                "Cannot switch from a configured archive backend "
                f"({current.backend!r}) back to 'disabled'.  Switch to "
                "'local' first if you want to step down from s3, then "
                "leave it there — disabling a once-configured backend "
                "would orphan archive data on disk or in S3 with no "
                "openhost-side handle to recover it."
            )

        # s3_prefix is the JuiceFS volume name: JuiceFS uses the volume
        # name as its per-object prefix, which is the only mechanism it
        # has for sub-bucket isolation.
        if target_backend == "s3":
            volume_name = s3_prefix or juicefs_volume_name or current.juicefs_volume_name or "openhost"
        else:
            volume_name = current.juicefs_volume_name or "openhost"

        _update_state(db, state_message="Stopping apps")

        # stop_app failures are fatal: a still-running app writing to the
        # source archive would corrupt the copy.
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

        # None when current.backend == 'disabled' (no on-disk source).
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
        if old_archive_dir is None:
            # disabled -> *: nothing on disk to migrate.
            teardown_warning = None
        else:
            _migrate_archive_data(db, plan, old_archive_dir, new_archive_dir)
            teardown_warning = _tear_down_source(config, db, plan, old_archive_dir)

        # Bypass _update_state because its "None means skip" rule would
        # let stale region/endpoint values from a previous s3 switch
        # silently persist and route the next mount at the wrong endpoint.
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
        elif target_backend == "local":
            # Drop creds; keep bucket/region/endpoint to pre-fill the
            # next switch-back-to-s3 form.
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
        else:
            # disabled→disabled is the no-op short-circuit and
            # local|s3→disabled is rejected upstream, so this is unreachable.
            raise BackendSwitchError(
                f"unreachable: target_backend={target_backend!r} reached the success-path SQL with no matching branch."
            )
        db.commit()

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
        # Wrap non-BackendSwitchError so the api layer sees one type.
        if isinstance(exc, BackendSwitchError):
            raise
        raise BackendSwitchError(str(exc)) from exc
    finally:
        # Always restart what we stopped; without this a failed switch
        # would orphan apps in 'stopped' forever (a retry would find
        # affected_apps empty since the apps are no longer running).
        # Iterate stopped_apps NOT affected_apps so we don't start apps
        # whose earlier stop_app raised.
        for name in stopped_apps:
            try:
                hook.start_app(name)
            except Exception:
                logger.exception("Failed to restart %s after backend switch", name)
