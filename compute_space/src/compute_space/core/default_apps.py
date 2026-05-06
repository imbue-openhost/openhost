"""Auto-deploy ``config.default_apps`` builtin apps at /setup completion."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import validate_manifest
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest

MAX_RETRY_ATTEMPTS = 3


@attr.s(auto_attribs=True, frozen=True)
class DefaultAppOutcome:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    attempts: int
    error: str | None = None


def _load_sentinel(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
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


def _install_one(dir_name: str, config: Config, db: sqlite3.Connection) -> tuple[str, str | None]:
    """Returns (status, error).  status in {"ok", "skipped", "failed"}."""
    app_dir = os.path.join(config.apps_dir, dir_name)
    if not os.path.isdir(app_dir) or not os.path.isfile(os.path.join(app_dir, "openhost.toml")):
        return "failed", f"builtin app dir or openhost.toml missing: {app_dir}"

    tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    clone_dir = os.path.join(tmp_parent, "repo")
    try:
        shutil.copytree(app_dir, clone_dir)
        manifest = parse_manifest(clone_dir)
    except Exception as exc:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "failed", f"clone/parse: {exc}"

    if db.execute("SELECT 1 FROM apps WHERE name = ?", (manifest.name,)).fetchone():
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "skipped", None

    validation_error = validate_manifest(manifest, db)
    if validation_error is not None:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "failed", validation_error

    final_dir = move_clone_to_app_temp_dir(clone_dir, manifest.name, config)
    try:
        insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            grant_permissions=set(),
            grant_permissions_v2=True,
            repo_url=f"file://{app_dir}",
        )
    except Exception as exc:
        return "failed", f"insert_and_deploy: {exc}"
    return "ok", None


def deploy_default_apps(config: Config, db: sqlite3.Connection) -> list[DefaultAppOutcome]:
    """Idempotent across boots.  ok/skipped are terminal; failed retries
    up to MAX_RETRY_ATTEMPTS.  Never raises."""
    if not config.default_apps:
        return []

    sentinel = _load_sentinel(config.default_apps_sentinel_path)
    outcomes: list[DefaultAppOutcome] = []

    for dir_name in config.default_apps:
        prior = sentinel.get(dir_name) or {}
        prior_status = prior.get("status")
        try:
            prior_attempts = int(prior.get("attempts", 0))
        except (TypeError, ValueError):
            prior_attempts = 0

        if prior_status in ("ok", "skipped"):
            outcomes.append(DefaultAppOutcome(dir_name, prior_status, prior_attempts))
            continue
        if prior_status == "failed" and prior_attempts >= MAX_RETRY_ATTEMPTS:
            outcomes.append(DefaultAppOutcome(dir_name, "failed", prior_attempts, prior.get("error")))
            continue

        try:
            status, error = _install_one(dir_name, config, db)
        except Exception as exc:
            status, error = "failed", f"unexpected: {exc}"

        attempts = prior_attempts + 1 if status == "failed" else 1
        outcomes.append(DefaultAppOutcome(dir_name, status, attempts, error))
        sentinel[dir_name] = {"status": status, "attempts": attempts, "error": error}

    _write_sentinel(config.default_apps_sentinel_path, sentinel)

    for o in outcomes:
        if o.status == "ok":
            logger.info("default_apps: %s deployed", o.name)
        elif o.status == "skipped":
            logger.info("default_apps: %s skipped (already installed)", o.name)
        else:
            logger.warning("default_apps: %s failed (attempt %d): %s", o.name, o.attempts, o.error)

    return outcomes
