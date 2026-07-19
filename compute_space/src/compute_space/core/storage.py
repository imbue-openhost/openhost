"""Storage usage helpers and the storage guard.

Reports disk usage totals and per-app breakdowns for the System page.

The storage guard runs as a daemon thread that periodically checks disk free
space and stops apps when free space drops below a threshold. Its enabled flag
and threshold are configured at runtime from the System page and persisted in
the ``storage_settings`` table; changes take effect without a restart. The guard
is enabled by default at ``DEFAULT_GUARD_MIN_FREE_MB`` MB, seeded into the table
by the v0012 migration / schema.sql. The legacy ``storage_min_free_mb`` config
key is only a boot-time seed that can raise the threshold (see
``seed_storage_settings_from_config``); it never disables the guard or overrides
the owner's enable/disable choice. The guard can be paused from the System page
so a user can start one app (e.g. a file browser) to clean up data before
resuming enforcement.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time

import attr

from compute_space.config import Config
from compute_space.core.containers import container_image_storage_bytes
from compute_space.core.containers import stop_app_process
from compute_space.core.logging import logger

_MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Runtime-configurable settings (persisted in the storage_settings table)
#
# The enabled flag and the MB threshold live in a single-row ``storage_settings``
# table and are read fresh on every guard check, so changes made from the System
# page take effect without a restart.
#
# The guard is enabled by default with a modest headroom threshold so that a
# runaway disk does not silently take an instance fully down before the owner
# ever hears of the guard. Owners can raise, lower, or disable it from the System
# page.
# ---------------------------------------------------------------------------

# Default minimum free space (MB) the guard enforces when enabled. This is the
# canonical value; the fresh-DB seed rows in the v0012 migration and schema.sql
# use the same number, and ``test_api_storage_settings.test_seed_default_matches_constant``
# asserts the seeded DB value stays in sync with this constant so they cannot drift.
DEFAULT_GUARD_MIN_FREE_MB = 1500


@attr.s(auto_attribs=True, frozen=True)
class StorageSettings:
    enabled: bool
    min_free_mb: int


def read_storage_settings(db: sqlite3.Connection) -> StorageSettings:
    """Read the guard settings from the single-row storage_settings table.

    Falls back to (disabled, 0) if the row is somehow missing (migrations
    seed it, so this is only defensive).
    """
    row = db.execute("SELECT enabled, min_free_mb FROM storage_settings WHERE id = 1").fetchone()
    if row is None:
        return StorageSettings(enabled=False, min_free_mb=0)
    return StorageSettings(enabled=bool(row[0]), min_free_mb=int(row[1]))


def write_storage_settings(db: sqlite3.Connection, *, enabled: bool, min_free_mb: int) -> StorageSettings:
    """Persist the guard settings. ``min_free_mb`` must be >= 0."""
    if min_free_mb < 0:
        raise ValueError("min_free_mb must be >= 0")
    db.execute(
        "UPDATE storage_settings SET enabled = ?, min_free_mb = ? WHERE id = 1",
        (1 if enabled else 0, int(min_free_mb)),
    )
    db.commit()
    return StorageSettings(enabled=enabled, min_free_mb=int(min_free_mb))


def seed_storage_settings_from_config(config: Config) -> None:
    """Raise the persisted threshold to a larger legacy ``storage_min_free_mb``
    config value, without ever overriding the owner's enable/disable choice.

    The guard ships enabled with ``DEFAULT_GUARD_MIN_FREE_MB`` (seeded by the
    migration / schema.sql), so operators no longer need the config key. But an
    operator who explicitly set a *larger* ``storage_min_free_mb`` in their
    router config clearly wants at least that much headroom, so we raise the
    stored threshold to match while leaving the enabled flag untouched.

    An owner's System-page choice always wins: if the owner disabled the guard,
    it stays disabled (we never re-enable it here); if the owner lowered the
    threshold below the legacy value, we still raise it (the config expresses a
    minimum headroom the operator requires) but do not re-enable a disabled
    guard. We never lower the threshold and never disable from the config.
    """
    legacy_mb = int(config.storage_min_free_mb)
    if legacy_mb <= 0:
        return
    db = sqlite3.connect(config.db_path)
    try:
        current = read_storage_settings(db)
        # Never lower the threshold from the config.
        if current.min_free_mb >= legacy_mb:
            return
        # Preserve the owner's enable/disable choice: only raise the threshold,
        # keeping ``enabled`` exactly as the owner left it. In particular a guard
        # the owner disabled from the UI must stay disabled.
        write_storage_settings(db, enabled=current.enabled, min_free_mb=legacy_mb)
        logger.info(
            "Raised storage guard threshold to legacy config storage_min_free_mb=%d (enabled=%s)",
            legacy_mb,
            current.enabled,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Guard state
# ---------------------------------------------------------------------------

_STORAGE_GUARD_INTERVAL_SECONDS = 60

_guard_paused: bool = False
_guard_lock = threading.Lock()
_guard_db_paths: set[str] = set()


def is_guard_paused() -> bool:
    with _guard_lock:
        return _guard_paused


def set_guard_paused(paused: bool) -> None:
    with _guard_lock:
        global _guard_paused
        _guard_paused = paused
    state = "paused" if paused else "resumed"
    logger.info("Storage guard %s", state)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_bytes(size: int) -> str:
    """Format bytes as a human-friendly string."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(size)} B"


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------


def _ensure_storage_roots(config: Config) -> None:
    os.makedirs(config.persistent_data_dir, exist_ok=True)
    os.makedirs(config.temporary_data_dir, exist_ok=True)
    os.makedirs(os.path.join(config.persistent_data_dir, "vm_data"), exist_ok=True)
    os.makedirs(os.path.join(config.persistent_data_dir, "app_data"), exist_ok=True)


def _dir_size_bytes(path: str) -> int:
    """Return total size of all files under *path* in bytes.

    There is no stdlib equivalent — ``shutil.disk_usage`` reports whole-disk
    stats, not per-directory totals.  Uses ``os.scandir`` recursion rather
    than ``os.walk`` + ``getsize`` so each file is stat'd once, not twice —
    this endpoint walks entire app data trees.  Symlinks are not followed.
    """
    try:
        entries = os.scandir(path)
    except OSError:
        return 0
    total = 0
    with entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += _dir_size_bytes(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _min_free_bytes_from_settings(settings: StorageSettings) -> int | None:
    """Effective minimum free space in bytes for the given settings, or None if
    the guard is inactive. The guard is active only when ``enabled`` is true AND
    ``min_free_mb`` is > 0."""
    if not settings.enabled or settings.min_free_mb <= 0:
        return None
    return settings.min_free_mb * _MIB


def storage_min_free_bytes(config: Config) -> int | None:
    """Return the effective minimum free space in bytes, or None if the guard
    is disabled / unset.

    Reads the runtime settings from the storage_settings table. Opening a
    short-lived connection here keeps every existing caller's ``(config)``
    signature unchanged while making the threshold live-configurable.
    """
    db = sqlite3.connect(config.db_path)
    try:
        settings = read_storage_settings(db)
    finally:
        db.close()
    return _min_free_bytes_from_settings(settings)


def disk_free_bytes(config: Config) -> int:
    """Return free bytes on the data disk."""
    _ensure_storage_roots(config)
    return shutil.disk_usage(config.data_root_dir).free


def openhost_data_usage_bytes(config: Config) -> int:
    _ensure_storage_roots(config)
    return _dir_size_bytes(os.path.join(config.persistent_data_dir, "vm_data"))


def per_app_usage(config: Config) -> dict[str, int]:
    """Return ``{app_name: bytes_used}`` for each subdirectory of app_data."""
    _ensure_storage_roots(config)
    app_data_root = os.path.join(config.persistent_data_dir, "app_data")
    result: dict[str, int] = {}
    if not os.path.isdir(app_data_root):
        return result
    for entry in os.listdir(app_data_root):
        entry_path = os.path.join(app_data_root, entry)
        if os.path.isdir(entry_path):
            result[entry] = _dir_size_bytes(entry_path)
    return result


def _app_data_loose_file_bytes(config: Config) -> int:
    """Size of files sitting directly in app_data (not inside a per-app dir)."""
    app_data_root = os.path.join(config.persistent_data_dir, "app_data")
    try:
        entries = os.scandir(app_data_root)
    except OSError:
        return 0
    total = 0
    with entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _check_min_free(config: Config) -> tuple[int, int] | None:
    """Return ``(free, min_free)`` if a threshold is set and free space is below it, else None."""
    min_free = storage_min_free_bytes(config)
    if min_free is None:
        return None
    free = disk_free_bytes(config)
    if free < min_free:
        return free, min_free
    return None


def storage_low(config: Config) -> bool:
    """Return True if a min-free threshold is set and free space is below it."""
    return _check_min_free(config) is not None


def check_before_deploy(config: Config) -> None:
    """Pre-deploy storage check: raise if free space is below the configured minimum."""
    result = _check_min_free(config)
    if result is not None:
        free, min_free = result
        raise RuntimeError(f"Storage too low ({format_bytes(free)} free, {format_bytes(min_free)} required)")


def storage_status(config: Config) -> dict[str, object]:
    """Return disk totals and usage for the System page / API.

    Both persistent and temporary data live on the same disk (under
    ``data_root_dir``), so we report a single unified disk metric.
    """
    _ensure_storage_roots(config)

    disk = shutil.disk_usage(config.data_root_dir)

    # Read the guard settings once and derive every settings-dependent field
    # from that single snapshot, so the reported values are mutually consistent
    # (a concurrent settings write can't split this response) and the System
    # page endpoint issues one DB read instead of three.
    db = sqlite3.connect(config.db_path)
    try:
        settings = read_storage_settings(db)
    finally:
        db.close()

    min_free = _min_free_bytes_from_settings(settings)
    storage_is_low = min_free is not None and disk.free < min_free

    # The app_data total is derived from the per-app walk rather than walked
    # again — these trees are large enough that a second pass is noticeable.
    per_app = per_app_usage(config)

    return {
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "openhost_data_used_bytes": openhost_data_usage_bytes(config),
        "app_data_used_bytes": sum(per_app.values()) + _app_data_loose_file_bytes(config),
        "build_cache_bytes": container_image_storage_bytes(),
        "per_app": per_app,
        "storage_min_free_bytes": min_free,
        "storage_low": storage_is_low,
        "guard_paused": is_guard_paused(),
        # Runtime-configurable guard settings (for the System page controls).
        "guard_enabled": settings.enabled,
        "guard_min_free_mb": settings.min_free_mb,
    }


# ---------------------------------------------------------------------------
# Storage guard — enforcement loop
# ---------------------------------------------------------------------------


def _stop_app_process_safe(row: sqlite3.Row) -> None:
    """Call stop_app_process, catching errors."""
    try:
        stop_app_process(row)
    except Exception:
        logger.exception("Failed to stop app %s", row["name"])


def enforce_storage_guard(config: Config) -> None:
    """Enforce minimum free disk space.

    If free space is below the threshold and the guard is not paused,
    stops all running apps.
    """
    result = _check_min_free(config)
    if result is None:
        return

    free, min_free = result
    logger.warning(
        "Storage low (%s free, %s required)",
        format_bytes(free),
        format_bytes(min_free),
    )

    if is_guard_paused():
        logger.info("Storage guard is paused; skipping app shutdown despite low storage")
        return

    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM apps WHERE status IN ('running', 'starting')").fetchall()
        if not rows:
            return

        for row in rows:
            detail = "Storage too low. Free space by removing app data or resizing disks."
            logger.warning("Stopping app %s due to low storage", row["name"])
            _stop_app_process_safe(row)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ?, container_id = NULL WHERE app_id = ?",
                (detail, row["app_id"]),
            )
            db.commit()
    finally:
        db.close()


def _storage_guard_loop(config: Config) -> None:
    while True:
        try:
            enforce_storage_guard(config)
        except Exception:
            logger.exception("Storage guard check failed")
        time.sleep(_STORAGE_GUARD_INTERVAL_SECONDS)


def start_storage_guard(config: Config) -> None:
    """Start the storage guard daemon thread (once per db_path).

    The thread always starts, so the loop is running to react when an owner
    enables the guard from the System page. Each iteration re-reads the
    persisted settings and is a cheap no-op while the guard is disabled or its
    threshold is unset (see ``enforce_storage_guard`` / ``_check_min_free``).
    """
    db_key = os.path.abspath(config.db_path)
    if db_key in _guard_db_paths:
        return
    _guard_db_paths.add(db_key)
    threading.Thread(target=_storage_guard_loop, args=(config,), daemon=True).start()
