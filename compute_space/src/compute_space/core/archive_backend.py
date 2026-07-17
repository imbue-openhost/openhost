"""Operator-controlled archive backend management.  See
``docs/src/data.md`` for the operator-facing model."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
import tomllib
from typing import Any

import attr
import boto3
import botocore.exceptions  # noqa: F401  -- imported for ``except`` matching downstream

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.core.pinned_binary import install_pinned_binary

# Name of the systemd unit that manages the JuiceFS FUSE mount.
# Installed by ansible (disabled); enabled by compute_space when the
# operator configures the archive backend.
JUICEFS_SERVICE = "openhost-juicefs"

# JuiceFS binary: pinned version + per-arch download URLs/checksums live in
# ``pinned_binary.py``.
_JUICEFS = get_pinned_binary("juicefs")


def _juicefs_state_dir(config: Config) -> str:
    """Critical state that must survive reboots (meta.db); back this up."""
    return os.path.join(config.openhost_data_path, "juicefs", "state")


def _juicefs_runtime_dir(config: Config) -> str:
    """Regenerable state (binary, etc.); safe to wipe."""
    return os.path.join(config.openhost_data_path, "juicefs", "runtime")


def _juicefs_install_dir(config: Config) -> str:
    return os.path.join(_juicefs_runtime_dir(config), "bin")


def _juicefs_binary(config: Config) -> str:
    return os.path.join(_juicefs_install_dir(config), f"juicefs-{_JUICEFS.version}")


def _juicefs_meta_db(config: Config) -> str:
    return os.path.join(_juicefs_state_dir(config), "meta.db")


def juicefs_mount_dir(config: Config) -> str:
    """The host-side JuiceFS FUSE mount; bind-mounted into containers."""
    return config.app_archive_dir


def local_archive_dir(config: Config) -> str:
    """The host-side local-disk backing for the archive tier (backend='local').

    A plain directory (NOT a mount), deliberately at a different path from
    the JuiceFS mountpoint so it can never be shadowed by a future S3 mount.
    """
    return config.local_archive_dir


def effective_archive_dir(config: Config, db: sqlite3.Connection) -> str:
    """The host path that should be bind-mounted into app containers as the
    archive tier, chosen by the current backend:

    * ``'s3'``   -> the JuiceFS mountpoint (``app_archive_dir``)
    * ``'local'``-> the local-disk directory (``local_archive_dir``)
    * ``'disabled'`` (legacy, pre-v12) -> the JuiceFS mountpoint, which is
      absent, so archive-using apps see "not available" exactly as before.

    Callers pass the result where they previously passed
    ``config.app_archive_dir`` so app provisioning / container mounts land
    on the right backing without any other code needing to know the backend.
    """
    state = read_state(db)
    if state.backend == "local":
        return local_archive_dir(config)
    return juicefs_mount_dir(config)


def ensure_local_archive_dir(config: Config) -> str:
    """Create the local archive directory if missing; return its path.

    Safe to call repeatedly.  Used at boot (attach_on_startup) and before
    provisioning archive-using apps in local mode.
    """
    path = local_archive_dir(config)
    os.makedirs(path, exist_ok=True)
    return path


def local_archive_has_data(config: Config) -> bool:
    """True iff any app has written content into the local archive dir.

    Used to decide whether an operator upgrading local -> S3 is about to
    migrate real data.  A directory that exists but is empty (no per-app
    subdirectories with content) counts as "no data".
    """
    root = local_archive_dir(config)
    if not os.path.isdir(root):
        return False
    for app_name in os.listdir(root):
        app_dir = os.path.join(root, app_name)
        if not os.path.isdir(app_dir):
            continue
        # Any entry inside a per-app archive dir counts as data.
        for _ in os.scandir(app_dir):
            return True
    return False


@attr.s(auto_attribs=True, frozen=True)
class StorageSummary:
    """The storage tiers an app will use, for the install screen.

    Mirrors the way permissions are surfaced before install: the operator
    sees, up front, which storage tiers the app touches and — crucially —
    whether its archive data will land on durable S3 or on non-durable
    LOCAL disk (so they can decide to configure S3 first if they care).
    """

    app_data: bool  # local, backed-up permanent data
    app_temp_data: bool  # local scratch, not backed up
    uses_archive: bool  # app_archive OR access_all_archive / access_all_data
    requires_archive: bool  # hard app_archive requirement
    archive_backend: str  # "local" | "s3" | "disabled"
    archive_is_durable: bool  # True only when backend == "s3"
    warnings: list[str]


def storage_summary(manifest_raw: str, db: sqlite3.Connection) -> StorageSummary:
    """Build the :class:`StorageSummary` for an app's manifest + current backend."""
    data = _data_section(manifest_raw)
    requires = bool(data.get("app_archive"))
    uses = bool(data.get("app_archive") or data.get("access_all_archive") or data.get("access_all_data"))
    backend = read_state(db).backend
    durable = backend == "s3"
    warnings: list[str] = []
    if uses and backend == "local":
        warnings.append(
            "This app stores bulk data on the ARCHIVE tier, which is currently "
            "backed by LOCAL disk on this instance. Local archive data is kept on "
            "the instance and included in backups, but it is NOT on durable object "
            "storage. Configure an S3 archive backend on the Settings page for "
            "durable, elastic storage; existing local data is migrated into S3 when "
            "you do."
        )
    return StorageSummary(
        app_data=bool(data.get("app_data", True)) or bool(data.get("sqlite")) or bool(data.get("access_all_app_data")),
        app_temp_data=bool(data.get("app_temp_data")) or bool(data.get("access_all_app_data")),
        uses_archive=uses,
        requires_archive=requires,
        archive_backend=backend,
        archive_is_durable=durable,
        warnings=warnings,
    )


def local_archive_apps_with_data(config: Config) -> list[str]:
    """Return the app names that have content in the local archive dir.

    Powers the operator-facing "these apps' archive data will be migrated"
    summary shown before a local -> S3 upgrade.
    """
    root = local_archive_dir(config)
    if not os.path.isdir(root):
        return []
    apps: list[str] = []
    for app_name in sorted(os.listdir(root)):
        app_dir = os.path.join(root, app_name)
        if not os.path.isdir(app_dir):
            continue
        if any(True for _ in os.scandir(app_dir)):
            apps.append(app_name)
    return apps


def juicefs_meta_db_path(config: Config) -> str:
    return _juicefs_meta_db(config)


def juicefs_state_dir(config: Config) -> str:
    return _juicefs_state_dir(config)


def is_juicefs_installed(config: Config) -> bool:
    return os.path.isfile(_juicefs_binary(config)) and os.access(_juicefs_binary(config), os.X_OK)


def install_juicefs(config: Config) -> None:
    """Download + verify + extract the JuiceFS binary.  Idempotent."""
    install_pinned_binary(_JUICEFS, _juicefs_binary(config))


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
    # JuiceFS's sqlite3 meta backend opens the file but won't mkdir its parent.
    os.makedirs(_juicefs_state_dir(config), exist_ok=True)
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


def _juicefs_env_file(config: Config) -> str:
    """Path to the systemd EnvironmentFile for the JuiceFS service.

    Contains JUICEFS_BINARY, JUICEFS_META_DSN, JUICEFS_MOUNT_DIR, and
    the S3 credentials (ACCESS_KEY / SECRET_KEY).  Written by
    ``_write_env_file`` at configure/attach time; read by the
    ``openhost-juicefs.service`` systemd unit.
    """
    return os.path.join(config.openhost_data_path, "juicefs", "juicefs.env")


def _write_env_file(
    config: Config,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> None:
    """Write (or overwrite) the systemd EnvironmentFile for JuiceFS.

    The file is mode 0600 so only the ``host`` user can read the S3
    credentials.  Parent directories are created if missing.
    """
    env_path = _juicefs_env_file(config)
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    content = (
        f"JUICEFS_BINARY={_juicefs_binary(config)}\n"
        f"JUICEFS_META_DSN={_format_meta_dsn(config)}\n"
        f"JUICEFS_MOUNT_DIR={juicefs_mount_dir(config)}\n"
        f"ACCESS_KEY={s3_access_key_id}\n"
        f"SECRET_KEY={s3_secret_access_key}\n"
    )
    # Atomic-ish write: write to a temp file then rename so a crash
    # mid-write doesn't leave a truncated env file.
    tmp_path = env_path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.rename(tmp_path, env_path)
    logger.info("Wrote JuiceFS env file at %s", env_path)


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a ``systemctl`` command and return the result.

    Raises ``RuntimeError`` on non-zero exit.
    """
    cmd = ["sudo", "systemctl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


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
    """Start the JuiceFS mount via systemd.  Idempotent.

    Writes the EnvironmentFile (binary path, meta DSN, mount dir, S3
    creds), then enables and starts the ``openhost-juicefs`` systemd
    service.  systemd's ``Restart=always`` handles automatic recovery
    if the FUSE process is OOM-killed or crashes.
    """
    mount_point = juicefs_mount_dir(config)
    os.makedirs(mount_point, exist_ok=True)

    if is_mounted(mount_point):
        logger.info("juicefs already mounted at %s", mount_point)
        return

    _write_env_file(config, s3_access_key_id, s3_secret_access_key)

    logger.info("Starting %s systemd service", JUICEFS_SERVICE)
    # daemon-reload in case the unit file was just installed or updated.
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", JUICEFS_SERVICE)

    # Wait for the mount to appear.  systemd starts the process
    # asynchronously; the FUSE handshake + initial S3 connection
    # can take 15-30s on high-latency links (e.g. Hetzner -> us-west-2).
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_mounted(mount_point):
            logger.info("juicefs mount ready at %s (via systemd)", mount_point)
            return
        time.sleep(0.5)

    # Check if the service failed to start.
    try:
        status = subprocess.run(
            ["systemctl", "is-active", JUICEFS_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        svc_state = status.stdout.strip()
    except Exception:
        svc_state = "unknown"

    raise RuntimeError(
        f"juicefs mount did not become ready within 30s at {mount_point} "
        f"(service state: {svc_state}); check journalctl -u {JUICEFS_SERVICE}"
    )


def umount(config: Config) -> None:
    """Stop the JuiceFS systemd service and unmount.

    Surfaces a failed-stop rather than swallowing it.  Idempotent.
    """
    mount_point = juicefs_mount_dir(config)

    if not is_mounted(mount_point):
        # Service might be stopped already; ensure it's disabled so it
        # doesn't auto-start on next boot.
        try:
            _systemctl("disable", "--now", JUICEFS_SERVICE)
        except RuntimeError:
            pass  # already stopped/disabled
        return

    logger.info("Stopping %s systemd service", JUICEFS_SERVICE)
    try:
        _systemctl("stop", JUICEFS_SERVICE, timeout=30)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to stop {JUICEFS_SERVICE}; ensure all containers "
            f"using the archive tier are stopped before switching "
            f"backends.  Original: {exc}"
        ) from exc

    _systemctl("disable", JUICEFS_SERVICE)
    logger.info("juicefs unmounted from %s (via systemd)", mount_point)


@attr.s(auto_attribs=True, frozen=True)
class BackendState:
    """Operator-visible archive backend state."""

    backend: str  # "local" (default) | "s3" | "disabled" (legacy pre-v12)
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    s3_prefix: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    juicefs_volume_name: str
    configured_at: str | None
    state_message: str | None


def read_state(db: sqlite3.Connection) -> BackendState:
    row = db.execute(
        "SELECT backend, s3_bucket, s3_region, s3_endpoint, "
        "s3_access_key_id, s3_secret_access_key, juicefs_volume_name, "
        "configured_at, state_message, s3_prefix FROM archive_backend WHERE id = 1"
    ).fetchone()
    if row is None:
        # Defensive fallback for a partial DB; migrations seed this row.
        # The default archive backend is 'local' (always-available archive
        # on local disk); operators upgrade to 's3' explicitly.
        return BackendState(
            backend="local",
            s3_bucket=None,
            s3_region=None,
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id=None,
            s3_secret_access_key=None,
            juicefs_volume_name="openhost",
            configured_at=None,
            state_message=None,
        )
    return BackendState(
        backend=row[0],
        s3_bucket=row[1],
        s3_region=row[2],
        s3_endpoint=row[3],
        s3_access_key_id=row[4],
        s3_secret_access_key=row[5],
        juicefs_volume_name=row[6] or "openhost",
        configured_at=row[7],
        state_message=row[8],
        s3_prefix=row[9],
    )


def _data_section(manifest_raw: str) -> dict[str, Any]:
    """Read just the ``[data]`` table out of a stored manifest.  Tolerant of
    parse errors (returns ``{}``) because callers gate behaviour on this and
    a corrupt row should fail closed."""
    if not manifest_raw:
        return {}
    try:
        return tomllib.loads(manifest_raw).get("data", {}) or {}
    except tomllib.TOMLDecodeError:
        return {}


def manifest_requires_archive(manifest_raw: str) -> bool:
    """``app_archive = true`` means the app cannot run without the archive tier."""
    return bool(_data_section(manifest_raw).get("app_archive"))


def manifest_uses_archive(manifest_raw: str) -> bool:
    """Return True if the app receives the archive mount.

    ``app_archive`` is a hard requirement; ``access_all_archive`` and the
    convenience alias ``access_all_data`` are permissive but still cause
    the app to receive the archive bind-mount when the tier is live, so
    destructive removal still needs the archive healthy to delete S3 bytes.
    """
    data = _data_section(manifest_raw)
    return bool(data.get("app_archive") or data.get("access_all_archive") or data.get("access_all_data"))


def is_archive_dir_healthy(config: Config, db: sqlite3.Connection) -> bool:
    """True iff the archive tier is usable on the host for the current backend.

    * ``'disabled'`` (legacy) — no archive data to protect, passes.
    * ``'local'`` — healthy iff the local archive directory exists (it is
      created at boot / before provisioning, so this is normally true).
    * ``'s3'`` — the JuiceFS mount must be live; otherwise operations that
      would silently orphan or skip S3-side data are blocked.
    """
    state = read_state(db)
    if state.backend == "s3":
        return is_mounted(juicefs_mount_dir(config))
    if state.backend == "local":
        return os.path.isdir(local_archive_dir(config))
    return True


def _set_state_message(db: sqlite3.Connection, message: str | None) -> None:
    db.execute("UPDATE archive_backend SET state_message = ? WHERE id = 1", (message,))
    db.commit()


def attach_on_startup(config: Config, db: sqlite3.Connection) -> None:
    """Bring the archive backend back online at boot.  Failures don't crash boot;
    they're surfaced via state_message so the dashboard stays reachable.

    With the systemd service (``openhost-juicefs.service``), the mount is
    normally started by systemd before this process even boots (the unit
    has ``Before=openhost.service``).  This function handles the case where
    the service hasn't started yet (first boot after configuration, or if
    the env file is stale/missing) by writing a fresh env file and
    ensuring the service is enabled + started.
    """
    state = read_state(db)
    if state.backend == "local":
        # Local backend needs no mount — just make sure the directory the
        # containers bind-mount actually exists on this boot.
        try:
            ensure_local_archive_dir(config)
            _set_state_message(db, None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create local archive dir on startup")
            _set_state_message(db, f"Failed to create local archive dir: {exc}")
        return
    if state.backend != "s3":
        return
    try:
        if not is_juicefs_installed(config):
            install_juicefs(config)
        if state.s3_access_key_id is None or state.s3_secret_access_key is None:
            raise RuntimeError(
                "S3 credentials are missing from the archive_backend row.  Re-configure "
                "the archive backend from the dashboard."
            )
        mount(config, state.s3_access_key_id, state.s3_secret_access_key)
        _set_state_message(db, None)
    except Exception as exc:
        logger.exception("Failed to attach archive backend on startup")
        _set_state_message(db, f"Failed to attach archive backend: {exc}")


def _s3_client(
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> Any:
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
    count: int
    latest_at: str | None
    latest_key: str | None


def list_meta_dumps(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str,
) -> MetaDumpSummary | None:
    """Summarise JuiceFS meta-dump objects.  None on error.  Caps at 1000 dumps.

    JuiceFS prefixes every object it writes with the volume name, so dumps land
    at ``<volume>/meta/`` — not under ``s3_prefix`` (which only ever *feeds* the
    volume name, and is null whenever the operator didn't set one).
    """
    volume = (juicefs_volume_name or "").strip("/")
    list_prefix = f"{volume}/meta/" if volume else "meta/"
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        resp = client.list_objects_v2(Bucket=s3_bucket, Prefix=list_prefix, MaxKeys=1000)
    except Exception:
        logger.exception("list_meta_dumps: list_objects_v2 failed")
        return None

    contents = resp.get("Contents") or []
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
    """Probe with ``head_bucket``.  None on success, else error string."""
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        client.head_bucket(Bucket=s3_bucket)
    except Exception as exc:
        return f"S3 reachability test failed: {exc}"
    return None


class BackendConfigureError(Exception):
    """Raised by ``configure_backend`` when configuration fails."""


# Python payload run INSIDE ``podman unshare`` to copy the local archive
# into the JuiceFS mount and verify it.  It must run in the container's
# user namespace because the archive files are owned by the mapped
# app subuids (apps write them through an ``:idmap`` bind mount); the
# plain ``host`` user that runs compute_space cannot read them, but
# ``podman unshare`` maps that user to namespace-root which can.
#
# CRITICAL — ownership preservation: the copy MUST reproduce each
# source file's uid/gid on the destination.  Otherwise every migrated
# file ends up owned by namespace-root (0:0) and the app containers —
# which run as their own mapped uids — can no longer WRITE their own
# archive data after the switch to S3 (verified: file-browser PUT ->
# 500).  ``shutil.copy2`` preserves mode+mtime but NOT ownership, so we
# explicitly ``os.chown`` every destination entry to match its source
# (dirs included, via a post-walk).  The payload copies src->dst,
# restores ownership, then verifies every source file exists in the
# dest with an identical size, printing ``MIGRATE_OK`` on success or
# ``MIGRATE_FAIL:<detail>`` and exiting non-zero otherwise.  It NEVER
# deletes the source.
_MIGRATE_PAYLOAD = r"""
import os, shutil, sys
src_root, dst_root = sys.argv[1], sys.argv[2]
if not os.path.isdir(src_root):
    print("MIGRATE_OK"); sys.exit(0)
def _chown_like(src_path, dst_path):
    st = os.lstat(src_path)
    try:
        os.chown(dst_path, st.st_uid, st.st_gid, follow_symlinks=False)
    except OSError:
        pass
try:
    for entry in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, entry)
        dst = os.path.join(dst_root, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
        else:
            os.makedirs(dst_root, exist_ok=True)
            shutil.copy2(src, dst)
    # Reproduce ownership across the whole destination tree, matching
    # each entry to its source counterpart (root, dirs, and files).
    _chown_like(src_root, dst_root)
    for dirpath, dirs, files in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        for name in dirs + files:
            srcp = os.path.join(dirpath, name)
            dstp = os.path.join(dst_root, name) if rel == "." else os.path.join(dst_root, rel, name)
            _chown_like(srcp, dstp)
except Exception as exc:
    print("MIGRATE_FAIL:copy error: %r" % (exc,)); sys.exit(1)
missing, mismatch = [], []
for dirpath, _dirs, files in os.walk(src_root):
    rel = os.path.relpath(dirpath, src_root)
    for fname in files:
        sf = os.path.join(dirpath, fname)
        df = os.path.join(dst_root, fname) if rel == "." else os.path.join(dst_root, rel, fname)
        if not os.path.isfile(df):
            missing.append(os.path.join(rel, fname)); continue
        try:
            if os.path.getsize(sf) != os.path.getsize(df):
                mismatch.append(os.path.join(rel, fname))
        except OSError as exc:
            mismatch.append("%s (%s)" % (os.path.join(rel, fname), exc))
if missing or mismatch:
    print("MIGRATE_FAIL:verify failed; missing=%s mismatch=%s" % (missing[:10], mismatch[:10]))
    sys.exit(1)
print("MIGRATE_OK")
"""


def _migrate_local_archive_into_mount(config: Config) -> None:
    """Copy every byte of the local archive dir into the (freshly mounted)
    JuiceFS mount, then verify the copy before the caller commits the switch.

    Runs the copy + verify inside ``podman unshare`` because the archive
    files are owned by the container-mapped ``www-data`` subuid and are not
    readable by the plain ``host`` user this process runs as.

    FAIL-OPEN CONTRACT: this must either fully succeed or raise.  It never
    deletes the local source — the caller deletes it only AFTER the DB row
    has flipped to 's3'.  So if anything here (or the subsequent commit)
    fails, the local data is still intact at ``local_archive_dir`` and the
    backend row is still ``'local'``, i.e. the operator's data remains
    available on local storage.

    Verification: after copying, every regular file present in the source
    must exist in the destination with an identical size.  A mismatch (short
    copy, JuiceFS write error, etc.) makes the payload exit non-zero so the
    migration aborts.
    """
    src_root = local_archive_dir(config)
    dst_root = juicefs_mount_dir(config)
    if not os.path.isdir(src_root):
        return  # nothing to migrate

    if _podman_available():
        # ``podman unshare`` enters the rootless user namespace so we can
        # read the mapped-uid files.  Generous but bounded timeout: large
        # archives on slow S3 links take a while, but a wedged copy must
        # not hang the configure request forever.
        cmd = ["podman", "unshare", "python3", "-c", _MIGRATE_PAYLOAD, src_root, dst_root]
        logger.info("migrating local archive %s -> %s (via podman unshare)", src_root, dst_root)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 60 * 60)
        out = (result.stdout or "").strip()
        if result.returncode != 0 or "MIGRATE_OK" not in out:
            detail = out or (result.stderr or "").strip() or f"exit {result.returncode}"
            raise RuntimeError(f"local->S3 archive migration failed: {detail} (local archive data left intact)")
        return

    # No podman (e.g. unit tests, or a non-rootless deployment): run the
    # same copy+verify in-process.  Ownership isn't an obstacle here.
    _copy_and_verify_in_process(src_root, dst_root)


def _podman_available() -> bool:
    try:
        return subprocess.run(["podman", "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _copy_and_verify_in_process(src_root: str, dst_root: str) -> None:
    """Pure-Python mirror of ``_MIGRATE_PAYLOAD`` for environments without
    podman.  Raises on any copy or verification failure; never deletes src.

    Preserves ownership (uid/gid) like the payload does, so migrated files
    stay writable by their owning app container.  chown may fail when the
    caller isn't privileged (e.g. plain unit tests) — tolerated there since
    ownership isn't meaningful in that context."""
    import shutil  # noqa: PLC0415

    def _chown_like(src_path: str, dst_path: str) -> None:
        st = os.lstat(src_path)
        try:
            os.chown(dst_path, st.st_uid, st.st_gid, follow_symlinks=False)
        except OSError:
            pass

    for entry in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, entry)
        dst = os.path.join(dst_root, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
        else:
            os.makedirs(dst_root, exist_ok=True)
            shutil.copy2(src, dst)
    _chown_like(src_root, dst_root)
    for dirpath, dirs, files in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        for name in dirs + files:
            srcp = os.path.join(dirpath, name)
            dstp = os.path.join(dst_root, name) if rel == "." else os.path.join(dst_root, rel, name)
            _chown_like(srcp, dstp)
    missing: list[str] = []
    mismatch: list[str] = []
    for dirpath, _dirs, files in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        for fname in files:
            sf = os.path.join(dirpath, fname)
            df = os.path.join(dst_root, fname) if rel == "." else os.path.join(dst_root, rel, fname)
            if not os.path.isfile(df):
                missing.append(os.path.join(rel, fname))
                continue
            try:
                if os.path.getsize(sf) != os.path.getsize(df):
                    mismatch.append(os.path.join(rel, fname))
            except OSError as exc:
                mismatch.append(f"{os.path.join(rel, fname)} ({exc})")
    if missing or mismatch:
        raise RuntimeError(
            "local->S3 archive migration verification failed; "
            f"missing={missing[:10]} mismatch={mismatch[:10]} "
            "(local archive data left intact)"
        )


def configure_backend(
    config: Config,
    db: sqlite3.Connection,
    *,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_prefix: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str | None = None,
) -> None:
    """Configure the archive backend to S3, in one shot.

    Allowed starting states: ``'local'`` (the default — the archive data
    currently lives on local disk and is MIGRATED into S3) or ``'disabled'``
    (legacy pre-v12 zone with no archive data — nothing to migrate).  Once
    the backend is ``'s3'`` it cannot be reconfigured.

    Steps: install juicefs, format the volume, mount it, migrate any local
    archive data into the mount + verify, atomically flip the DB row to
    's3', then delete the now-copied local source.

    FAIL-OPEN: if any step before the DB flip fails, the local archive data
    is left untouched and the backend stays ``'local'``, so the operator's
    files remain available.  The local source is deleted only after the
    switch to 's3' has been committed AND the copy verified.
    """
    state = read_state(db)
    if state.backend == "s3":
        raise BackendConfigureError("archive backend is already configured to S3; reconfiguration is not supported")
    if state.backend not in ("local", "disabled"):
        raise BackendConfigureError(f"cannot configure S3 from backend={state.backend!r}")

    migrating_from_local = state.backend == "local"
    volume = juicefs_volume_name or s3_prefix or "openhost"

    try:
        if not is_juicefs_installed(config):
            install_juicefs(config)
        format_volume(
            config,
            s3_bucket=s3_bucket,
            s3_region=s3_region,
            s3_endpoint=s3_endpoint,
            s3_access_key_id=s3_access_key_id,
            s3_secret_access_key=s3_secret_access_key,
            juicefs_volume_name=volume,
        )
        mount(config, s3_access_key_id, s3_secret_access_key)

        # Migrate local data into the fresh mount BEFORE flipping the row.
        # If this raises, the except-block below leaves the backend at
        # 'local' with the source intact (fail-open).
        if migrating_from_local:
            _migrate_local_archive_into_mount(config)

        # Persist the new state.  If this fails after a successful
        # mount we leave a "live mount + local/disabled DB row"
        # inconsistency, but a subsequent configure_backend call retries
        # idempotently (format + mount are no-ops on an already-formatted
        # bucket and an already-live mount; the copy is dirs_exist_ok).
        db.execute(
            "UPDATE archive_backend SET "
            "backend='s3', s3_bucket=?, s3_region=?, s3_endpoint=?, s3_prefix=?, "
            "s3_access_key_id=?, s3_secret_access_key=?, juicefs_volume_name=?, "
            "configured_at=?, state_message=NULL "
            "WHERE id = 1",
            (
                s3_bucket,
                s3_region,
                s3_endpoint,
                s3_prefix,
                s3_access_key_id,
                s3_secret_access_key,
                volume,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        db.commit()
    except Exception as exc:
        # Best-effort: tear the mount back down so a retry starts clean and
        # the local backend keeps working.  Never touch the local source.
        if migrating_from_local:
            try:
                umount(config)
            except Exception:  # noqa: BLE001
                logger.exception("failed to unmount juicefs after aborted migration")
        raise BackendConfigureError(f"Failed to configure S3 archive backend: {exc}") from exc

    # DB is now committed to 's3' and the copy is verified.  Only now is it
    # safe to reclaim the local source.  A failure here is non-fatal: the
    # data is already durably in S3; the stale local dir just wastes disk.
    if migrating_from_local:
        _remove_local_archive_tree(config)


def _remove_local_archive_tree(config: Config) -> None:
    """Delete the local archive directory after a successful migration.

    Like the copy, the files are owned by the container-mapped www-data
    subuid, so a plain ``shutil.rmtree`` as the host user can't remove
    them — use ``podman unshare rm -rf`` (namespace-root).  Falls back to
    in-process rmtree when podman is unavailable.  Non-fatal: the data is
    already durably in S3; a stale local dir just wastes disk.
    """
    path = local_archive_dir(config)
    if not os.path.isdir(path):
        return
    try:
        if _podman_available():
            result = subprocess.run(
                ["podman", "unshare", "rm", "-rf", path],
                capture_output=True,
                text=True,
                timeout=10 * 60,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "").strip() or f"exit {result.returncode}")
            # ``rm -rf <path>`` removes path itself; recreate the empty root
            # so future reads of local_archive_dir don't hit a missing dir.
            os.makedirs(path, exist_ok=True)
        else:
            import shutil  # noqa: PLC0415

            shutil.rmtree(path, ignore_errors=False)
            os.makedirs(path, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception(
            "migrated local archive to S3 but failed to remove the local source at %s; safe to delete manually",
            path,
        )
