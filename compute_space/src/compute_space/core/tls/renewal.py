"""Background renewal daemon for per-app TLS certs (see :func:`renew_app_certs_once`)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest
from compute_space.core.tls.app_certs import app_cert_dir
from compute_space.core.tls.app_certs import provision_app_certs_for_deploy

# Check daily; the renewal window (30 days) gives ample slack so a missed
# sweep or two doesn't matter.
_RENEWAL_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60

_sweep_db_paths: set[str] = set()
_sweep_lock = threading.Lock()


def _dir_mtimes(path: str) -> dict[str, float]:
    """Snapshot of cert-file mtimes under ``path`` (recursive)."""
    out: dict[str, float] = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                out[fp] = os.path.getmtime(fp)
            except OSError:
                pass
    return out


def renew_app_certs_once(config: Config) -> None:
    """Re-provision certs for every running app with ``[[tls_certs]]``.

    App certs are ~90-day ACME certs; :func:`provision_app_certs_for_deploy`
    re-issues any within the renewal window, but a long-running app that is
    never reloaded would eventually present an expired cert, hence this sweep.
    Restarts an app's container only when its cert files actually changed
    (i.e. a renewal occurred) so the app loads the new pair (apps generally
    read certs only at startup).  Each app is handled independently; a failure
    on one is logged and does not block the others.
    """
    # Local import avoids a module-load import cycle (apps -> tls.app_certs).
    from compute_space.core.apps import start_app_process  # noqa: PLC0415

    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        rows = db.execute("SELECT * FROM apps WHERE status = 'running'").fetchall()
        for row in rows:
            app_name = row["name"]
            repo_path = row["repo_path"]
            if not repo_path or not os.path.isdir(repo_path):
                continue
            try:
                manifest = parse_manifest(repo_path)
            except Exception:
                logger.exception("Cert renewal: could not parse manifest for %s", app_name)
                continue
            if not manifest.tls_certs:
                continue
            cert_dir = str(app_cert_dir(config.openhost_data_path, app_name))
            before = _dir_mtimes(cert_dir)
            try:
                provision_app_certs_for_deploy(app_name, manifest, config)
            except Exception:
                logger.exception("Cert renewal failed for %s", app_name)
                continue
            after = _dir_mtimes(cert_dir)
            if after != before:
                logger.info("App %s cert renewed; restarting container to load new cert", app_name)
                try:
                    start_app_process(row["app_id"], db, config)
                except Exception:
                    logger.exception("Failed to restart %s after cert renewal", app_name)
    finally:
        db.close()


def _renewal_sweep_loop(config: Config) -> None:
    while True:
        try:
            renew_app_certs_once(config)
        except Exception:
            logger.exception("App cert renewal sweep failed")
        time.sleep(_RENEWAL_SWEEP_INTERVAL_SECONDS)


def start_cert_renewal_sweep(config: Config) -> None:
    """Start the per-app cert renewal daemon thread (once per db_path).

    No-op when TLS isn't enabled (no certs to renew).
    """
    if not config.tls_enabled:
        return
    db_key = os.path.abspath(config.db_path)
    with _sweep_lock:
        if db_key in _sweep_db_paths:
            return
        _sweep_db_paths.add(db_key)
    threading.Thread(target=_renewal_sweep_loop, args=(config,), daemon=True).start()
