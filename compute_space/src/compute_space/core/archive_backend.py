"""Operator-controlled archive backend management.  See
``docs/data.md`` for the operator-facing model."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
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


def juicefs_mount_dir(config: Config) -> str:
    """The host-side JuiceFS FUSE mount; bind-mounted into containers."""
    return config.app_archive_dir


def juicefs_meta_db_path(config: Config) -> str:
    return _juicefs_meta_db(config)


def juicefs_state_dir(config: Config) -> str:
    return _juicefs_state_dir(config)


def is_juicefs_installed(config: Config) -> bool:
    return os.path.isfile(_juicefs_binary(config)) and os.access(_juicefs_binary(config), os.X_OK)


def install_juicefs(config: Config) -> None:
    """Download + verify + extract the JuiceFS binary.  Idempotent."""
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
            # -o allow_other: rootless podman maps container uids into a
            # subuid range (e.g. container 1000 -> host 100999), so without
            # allow_other those container processes can't traverse the FUSE
            # mount at all.  POSIX dir-mode permissions on per-app subdirs
            # still gate writes; this just opens up FUSE-level traversal.
            # Requires user_allow_other in /etc/fuse.conf, which the
            # ansible setup playbook configures.
            "-o",
            "allow_other",
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

    backend: str  # "disabled" | "s3"
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
        return BackendState(
            backend="disabled",
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
    """``app_archive`` OR ``access_all_data`` means the app gets the archive mount."""
    data = _data_section(manifest_raw)
    return bool(data.get("app_archive") or data.get("access_all_data"))


def is_archive_dir_healthy(config: Config, db: sqlite3.Connection) -> bool:
    """True iff the configured archive backing is live on the host."""
    state = read_state(db)
    if state.backend != "s3":
        return False
    return is_mounted(juicefs_mount_dir(config))


def _set_state_message(db: sqlite3.Connection, message: str | None) -> None:
    db.execute("UPDATE archive_backend SET state_message = ? WHERE id = 1", (message,))
    db.commit()


def attach_on_startup(config: Config, db: sqlite3.Connection) -> None:
    """Bring the archive backend back online at boot.  Failures don't crash boot;
    they're surfaced via state_message so the dashboard stays reachable."""
    state = read_state(db)
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
    s3_prefix: str | None,
) -> MetaDumpSummary | None:
    """Summarise JuiceFS meta-dump objects.  None on error.  Caps at 1000 dumps."""
    prefix = (s3_prefix or "").strip("/")
    list_prefix = f"{prefix}/meta/" if prefix else "meta/"
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
    """Configure the archive backend to S3, in one shot.  Refuses to run unless
    the current backend is ``'disabled'`` — there is no in-product migration
    path back; an operator who picked S3 is committed.

    Steps: install juicefs, format the volume, mount it, then atomically
    UPDATE the archive_backend row.  No app-stop, no data-copy, no rollback
    — there is no source to migrate from.
    """
    state = read_state(db)
    if state.backend != "disabled":
        raise BackendConfigureError(
            f"archive backend is already configured (backend={state.backend!r}); reconfiguration is not supported"
        )

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
        # Persist the new state.  If this fails after a successful
        # mount we leave a "live mount + disabled DB row" inconsistency,
        # but a subsequent configure_backend call retries idempotently
        # (format + mount are no-ops on an already-formatted bucket and
        # an already-live mount).
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
        raise BackendConfigureError(f"Failed to configure S3 archive backend: {exc}") from exc
