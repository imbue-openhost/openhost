"""Periodic dangling-image pruner.

Every app rebuild re-tags ``openhost-{app}:latest`` and orphans the previous
image, so untagged (dangling) layers accumulate over time.  Nothing prunes
them automatically today: the ``/api/drop-docker-cache`` endpoint exists but
runs only on explicit operator action, so on long-lived hosts these dangling
images pile up and can fill the disk.

This module runs a lightweight daemon thread that periodically prunes *only*
dangling images (``podman image prune`` without ``--all``), independent of disk
pressure.  Tagged images for stopped apps are kept, so nothing has to be
rebuilt on next start — which is what makes an unconditional periodic prune
safe.  The interval is configured by ``image_prune_interval_seconds`` (0
disables the thread).
"""

from __future__ import annotations

import os
import threading
import time

from compute_space.config import Config
from compute_space.core.containers import prune_dangling_images
from compute_space.core.logging import logger

# Guard so the thread is started at most once per config (mirrors the storage
# guard).  Keyed by db_path because that is the stable per-instance identifier
# already used by the storage guard, and tests can clear it between runs.
_pruner_lock = threading.Lock()
_pruner_db_paths: set[str] = set()


def prune_interval_seconds(config: Config) -> int | None:
    """Return the configured prune interval in seconds, or None if disabled."""
    interval = int(config.image_prune_interval_seconds)
    if interval <= 0:
        return None
    return interval


def _run_prune_once() -> None:
    """Prune dangling images once, logging (never raising) on failure."""
    try:
        output = prune_dangling_images()
        if output:
            logger.info("Periodic image prune: %s", output)
    except Exception:
        logger.exception("Periodic image prune failed")


def _image_pruner_loop(interval: int) -> None:
    # Sleep first so we don't prune during the startup rush (when a deploy may
    # be mid-build and its intermediate layers are legitimately "dangling").
    while True:
        time.sleep(interval)
        _run_prune_once()


def start_image_pruner(config: Config) -> None:
    """Start the periodic image-pruner daemon thread (once per db_path).

    Only starts if ``image_prune_interval_seconds`` is configured (> 0).
    """
    interval = prune_interval_seconds(config)
    if interval is None:
        return
    db_key = os.path.abspath(config.db_path)
    with _pruner_lock:
        if db_key in _pruner_db_paths:
            return
        _pruner_db_paths.add(db_key)
    logger.info("Starting periodic image pruner (every %ds)", interval)
    threading.Thread(target=_image_pruner_loop, args=(interval,), daemon=True).start()
