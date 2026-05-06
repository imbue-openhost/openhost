"""Auto-deploy a configurable list of builtin apps at /setup completion.

The owner clicks through /setup once per zone; this module runs at the
tail of that handler and queues the apps named in ``Config.default_apps``
through the same ``insert_and_deploy`` path the dashboard uses.

Idempotency: a JSON sentinel at ``Config.default_apps_sentinel_path``
records per-app outcomes.  Apps that already have a row in the ``apps``
table are skipped silently; apps that previously failed get retried up
to ``MAX_RETRY_ATTEMPTS`` times across boots before being marked
permanently failed (operator can clear by deleting the sentinel).
Successful apps are never re-installed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.apps import clone_and_read_manifest_sync
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import validate_manifest
from compute_space.core.logging import logger

# Matches the existing convention for "give up rather than thrash" loops
# elsewhere in core/apps.py (build_image retries, container start retries).
MAX_RETRY_ATTEMPTS = 3


@attr.s(auto_attribs=True, frozen=True)
class DefaultAppOutcome:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    attempts: int
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class DefaultAppsResult:
    outcomes: list[DefaultAppOutcome]

    @property
    def ok_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "ok")

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


def _load_sentinel(path: str) -> dict[str, dict[str, Any]]:
    """Returns ``{<dir-name>: {"status": ..., "attempts": ...}}`` or ``{}``.

    Tolerates a missing or malformed sentinel — a zone with a hand-edited,
    truncated, or non-UTF-8 file should still get its defaults installed,
    not crash the /setup or startup hook.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict) and "status" in v}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("default_apps sentinel at %s is unreadable; ignoring", path)
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict) and "status" in v}
    except (OSError, json.JSONDecodeError):
        logger.warning("default_apps sentinel at %s is unreadable; ignoring", path)
        return {}


def _write_sentinel(path: str, state: dict[str, dict[str, Any]]) -> None:
    """Persist ``state`` to ``path``.  Best-effort: if the write fails the
    caller continues; the worst case is that we re-attempt successful
    installs on next boot, which then short-circuit on the existing-app
    check inside ``_install_one``.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write default_apps sentinel %s: %s", path, exc)


def _install_one(
    dir_name: str,
    config: Config,
    db: sqlite3.Connection,
) -> DefaultAppOutcome:
    """Install one builtin app by directory name; idempotent.

    Returns ``DefaultAppOutcome``; does not raise.  Per-app failures are
    captured in the outcome string so the caller can record them in the
    sentinel and let the operator retry from logs.
    """
    app_dir = os.path.join(config.apps_dir, dir_name)
    if not os.path.isdir(app_dir):
        return DefaultAppOutcome(
            name=dir_name,
            status="failed",
            attempts=1,
            error=f"builtin app dir not found: {app_dir}",
        )
    if not os.path.isfile(os.path.join(app_dir, "openhost.toml")):
        return DefaultAppOutcome(
            name=dir_name,
            status="failed",
            attempts=1,
            error=f"missing openhost.toml in {app_dir}",
        )

    repo_url = f"file://{app_dir}"
    manifest, clone_dir, err = clone_and_read_manifest_sync(repo_url)
    if err is not None or manifest is None or clone_dir is None:
        return DefaultAppOutcome(
            name=dir_name,
            status="failed",
            attempts=1,
            error=err or "manifest clone returned no manifest",
        )

    # ``clone_and_read_manifest_sync`` mkdtemp's a parent directory
    # and returns the ``repo`` subdir inside it; the parent has to
    # be cleaned up explicitly by the caller after a ``shutil.move``
    # of the subdir or it leaks an empty directory.
    tmp_parent = os.path.dirname(clone_dir)

    try:
        # Existing-row check: covers the case where an operator
        # manually installed the same app between boots, or where a
        # previous run of this hook succeeded but the sentinel got
        # truncated.
        existing = db.execute("SELECT name FROM apps WHERE name = ?", (manifest.name,)).fetchone()
        if existing is not None:
            return DefaultAppOutcome(name=dir_name, status="skipped", attempts=0)

        validation_error = validate_manifest(manifest, db)
        if validation_error is not None:
            return DefaultAppOutcome(
                name=dir_name,
                status="failed",
                attempts=1,
                error=f"manifest validation: {validation_error}",
            )

        # Move the temp clone into the persistent app_temp_data
        # location the dashboard would use; matches the side effects
        # of ``api_add_app`` at compute_space/web/routes/api/apps.py.
        # Future ``oh app reload`` invocations re-copy from
        # ``apps_dir`` for builtins anyway.
        final_dir = os.path.join(config.temporary_data_dir, "app_temp_data", manifest.name, "repo")
        if os.path.exists(final_dir):
            shutil.rmtree(final_dir, ignore_errors=True)
        os.makedirs(os.path.dirname(final_dir), exist_ok=True)
        shutil.move(clone_dir, final_dir)
        # ``shutil.move`` consumed clone_dir; null it so the finally
        # block doesn't try to rmtree the parent's now-empty content
        # twice.  ``tmp_parent`` itself is still an empty dir we
        # need to clean up.
        clone_dir = None  # noqa: F841

        try:
            insert_and_deploy(
                manifest,
                final_dir,
                config,
                db,
                grant_permissions=set(),
                grant_permissions_v2=True,
                repo_url=repo_url,
            )
        except Exception as exc:
            return DefaultAppOutcome(
                name=dir_name,
                status="failed",
                attempts=1,
                error=f"insert_and_deploy: {exc}",
            )

        return DefaultAppOutcome(name=dir_name, status="ok", attempts=1)
    finally:
        # Best-effort cleanup of the mkdtemp parent.  Ignored on
        # error because a leaked tempdir is much less bad than a
        # crash in the /setup hook.
        try:
            shutil.rmtree(tmp_parent, ignore_errors=True)
        except OSError:
            pass


def deploy_default_apps(config: Config, db: sqlite3.Connection) -> DefaultAppsResult:
    """Deploy each app in ``config.default_apps`` exactly once across boots.

    Sentinel-driven: apps marked ``ok`` or ``skipped`` are terminal
    and never re-attempted; apps marked ``failed`` are retried up to
    ``MAX_RETRY_ATTEMPTS`` total attempts across boots.  An app with
    no sentinel entry is installed as-new.

    Per-app outcomes from this invocation are returned with the
    sentinel-persisted status (``ok``/``skipped``/``failed``).  Never
    raises — partial failures are captured in the result, not
    propagated, so a misconfigured ``default_apps`` entry can't brick
    the /setup flow.
    """
    if not config.default_apps:
        return DefaultAppsResult(outcomes=[])

    sentinel = _load_sentinel(config.default_apps_sentinel_path)
    outcomes: list[DefaultAppOutcome] = []

    for dir_name in config.default_apps:
        prior = sentinel.get(dir_name, {})
        prior_status = prior.get("status")
        # Defensive: a hand-edited sentinel can put any value here.
        try:
            prior_attempts = int(prior.get("attempts", 0))
        except (TypeError, ValueError):
            prior_attempts = 0

        # ``ok`` and ``skipped`` are both terminal — once an app has
        # been resolved, never re-walk apps_dir for it.  Re-running
        # ``_install_one`` would just re-check the DB and return
        # ``skipped`` anyway; the early return saves the manifest
        # parse + tempdir copy on every restart.
        if prior_status in ("ok", "skipped"):
            outcomes.append(DefaultAppOutcome(name=dir_name, status=prior_status, attempts=prior_attempts))
            continue
        if prior_status == "failed" and prior_attempts >= MAX_RETRY_ATTEMPTS:
            outcomes.append(
                DefaultAppOutcome(
                    name=dir_name,
                    status="failed",
                    attempts=prior_attempts,
                    error=prior.get("error", "exhausted retries"),
                )
            )
            continue

        try:
            outcome = _install_one(dir_name, config, db)
        except Exception as exc:  # pragma: no cover - defensive
            outcome = DefaultAppOutcome(
                name=dir_name,
                status="failed",
                attempts=prior_attempts + 1,
                error=f"unexpected: {exc}",
            )
        else:
            if outcome.status == "failed":
                outcome = attr.evolve(outcome, attempts=prior_attempts + 1)

        outcomes.append(outcome)

        sentinel[dir_name] = {
            "status": outcome.status,
            "attempts": outcome.attempts,
            "error": outcome.error,
        }

    _write_sentinel(config.default_apps_sentinel_path, sentinel)

    for o in outcomes:
        if o.status == "ok":
            logger.info("default_apps: %s deployed", o.name)
        elif o.status == "skipped":
            logger.info("default_apps: %s skipped (already installed)", o.name)
        else:
            logger.warning("default_apps: %s failed (attempt %d): %s", o.name, o.attempts, o.error)

    return DefaultAppsResult(outcomes=outcomes)
