"""Auto-deploy ``config.default_apps`` builtin apps at /setup completion."""

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


def _write_sentinel(path: str, state: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write default_apps sentinel %s: %s", path, exc)


def _install_one(dir_name: str, config: Config, db: sqlite3.Connection) -> DefaultAppOutcome:
    app_dir = os.path.join(config.apps_dir, dir_name)
    if not os.path.isdir(app_dir):
        return DefaultAppOutcome(dir_name, "failed", 1, f"builtin app dir not found: {app_dir}")
    if not os.path.isfile(os.path.join(app_dir, "openhost.toml")):
        return DefaultAppOutcome(dir_name, "failed", 1, f"missing openhost.toml in {app_dir}")

    repo_url = f"file://{app_dir}"
    manifest, clone_dir, err = clone_and_read_manifest_sync(repo_url)
    if err is not None or manifest is None or clone_dir is None:
        return DefaultAppOutcome(dir_name, "failed", 1, err or "clone returned no manifest")

    tmp_parent = os.path.dirname(clone_dir)
    try:
        existing = db.execute("SELECT name FROM apps WHERE name = ?", (manifest.name,)).fetchone()
        if existing is not None:
            return DefaultAppOutcome(dir_name, "skipped", 0)

        validation_error = validate_manifest(manifest, db)
        if validation_error is not None:
            return DefaultAppOutcome(dir_name, "failed", 1, f"manifest validation: {validation_error}")

        final_dir = os.path.join(config.temporary_data_dir, "app_temp_data", manifest.name, "repo")
        if os.path.exists(final_dir):
            shutil.rmtree(final_dir, ignore_errors=True)
        os.makedirs(os.path.dirname(final_dir), exist_ok=True)
        shutil.move(clone_dir, final_dir)

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
            return DefaultAppOutcome(dir_name, "failed", 1, f"insert_and_deploy: {exc}")

        return DefaultAppOutcome(dir_name, "ok", 1)
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)


def deploy_default_apps(config: Config, db: sqlite3.Connection) -> DefaultAppsResult:
    """Idempotent across boots.  ok/skipped are terminal; failed retries
    up to MAX_RETRY_ATTEMPTS.  Never raises."""
    if not config.default_apps:
        return DefaultAppsResult(outcomes=[])

    sentinel = _load_sentinel(config.default_apps_sentinel_path)
    outcomes: list[DefaultAppOutcome] = []

    for dir_name in config.default_apps:
        prior = sentinel.get(dir_name, {})
        prior_status = prior.get("status")
        try:
            prior_attempts = int(prior.get("attempts", 0))
        except (TypeError, ValueError):
            prior_attempts = 0

        if prior_status in ("ok", "skipped"):
            outcomes.append(DefaultAppOutcome(dir_name, prior_status, prior_attempts))
            continue
        if prior_status == "failed" and prior_attempts >= MAX_RETRY_ATTEMPTS:
            outcomes.append(
                DefaultAppOutcome(
                    dir_name,
                    "failed",
                    prior_attempts,
                    prior.get("error", "exhausted retries"),
                )
            )
            continue

        try:
            outcome = _install_one(dir_name, config, db)
        except Exception as exc:
            outcome = DefaultAppOutcome(dir_name, "failed", prior_attempts + 1, f"unexpected: {exc}")
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
