"""App lifecycle operations: clone, deploy, start, stop, reload, validate.

Extracted from routes/apps.py — no HTTP/Quart dependencies.
"""

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse

import attr
import httpx

import compute_space.core.storage as storage
from compute_space.config import Config
from compute_space.core.containers import build_image
from compute_space.core.containers import build_log_path
from compute_space.core.containers import compute_uid_map_base
from compute_space.core.containers import run_container
from compute_space.core.data import provision_data
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping
from compute_space.core.manifest import parse_manifest
from compute_space.core.permissions import grant_permissions as grant_permissions_fn
from compute_space.core.ports import allocate_port
from compute_space.core.ports import resolve_port_mappings
from compute_space.core.services import OAuthAuthorizationRequired
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_oauth_token

RESERVED_PATHS = {
    "/",
    "/dashboard",
    "/login",
    "/logout",
    "/add_app",
    "/remove_app",
    "/stop_app",
    "/reload_app",
    "/api",
    "/health",
    "/app",
    "/setup",
    "/.well-known",
    "/handle_invite",
    "/terminal",
    "/toggle-ssh",
    "/identity",
    "/settings",
}


def list_builtin_apps(config: Config) -> list[dict[str, str]]:
    """Return list of dicts with name/url for each app in the builtin apps dir.

    Apps with ``hidden = true`` in their ``[app]`` section are excluded so they
    don't appear on the dashboard but remain deployable via direct URL.
    """
    builtin: list[dict[str, str]] = []
    if not os.path.isdir(config.apps_dir):
        return builtin
    for entry in sorted(os.listdir(config.apps_dir)):
        app_dir = os.path.join(config.apps_dir, entry)
        manifest_path = os.path.join(app_dir, "openhost.toml")
        if not os.path.isfile(manifest_path):
            continue
        try:
            manifest = parse_manifest(app_dir)
            if manifest.hidden:
                continue
        except Exception:
            # Unparseable manifest — still list it so the user sees the error
            # at deploy time rather than the app silently disappearing.
            pass
        builtin.append({"name": entry, "url": f"file://{app_dir}"})
    return builtin


def inject_github_token_in_url(url: str, token: str) -> str:
    """Inject a GitHub OAuth token into an HTTP(S) URL for authentication."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host_port = parsed.hostname
        if parsed.port:
            host_port = f"{parsed.hostname}:{parsed.port}"
        return parsed._replace(netloc=f"{token}@{host_port}").geturl()
    return url


async def clone_and_read_manifest(
    repo_url: str, github_token: str | None = None
) -> tuple[AppManifest | None, str | None, str | None]:
    """Clone a repo to a temp dir and read its openhost.toml.

    Returns (manifest, clone_dir, error). On success error is None.
    """
    base_url, ref = parse_repo_url(repo_url)
    clone_url = base_url

    # For file:// URLs, copy the directory if it's not a git repo
    if base_url.startswith("file://"):
        local_path = base_url[len("file://") :]
        if not os.path.isdir(local_path):
            return None, None, f"Local path does not exist: {local_path}"
        is_git = os.path.isdir(os.path.join(local_path, ".git")) or os.path.isfile(os.path.join(local_path, "HEAD"))
        if not is_git:
            tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
            clone_dir = os.path.join(tmp_parent, "repo")
            try:
                shutil.copytree(local_path, clone_dir)
                manifest = parse_manifest(clone_dir)
                return manifest, clone_dir, None
            except ValueError as e:
                shutil.rmtree(tmp_parent, ignore_errors=True)
                return None, None, str(e)
            except Exception as e:
                shutil.rmtree(tmp_parent, ignore_errors=True)
                return None, None, f"Copy failed: {e}"

    if github_token:
        clone_url = inject_github_token_in_url(base_url, github_token)

    tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    clone_dir = os.path.join(tmp_parent, "repo")
    try:
        clone_cmd = ["git", "clone"]
        if ref:
            clone_cmd.extend(["--branch", ref])
        clone_cmd.extend([clone_url, clone_dir])
        result = await asyncio.to_thread(subprocess.run, clone_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            shutil.rmtree(tmp_parent, ignore_errors=True)
            return None, None, f"Git clone failed: {result.stderr.strip()}"
        # If we cloned with a token, reset the remote URL so the token isn't persisted
        if github_token and clone_url != base_url:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "set-url", "origin", base_url],
                cwd=clone_dir,
                capture_output=True,
                timeout=10,
            )
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            shutil.rmtree(tmp_parent, ignore_errors=True)
            return None, None, str(e)
        return manifest, clone_dir, None
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return None, None, "Git clone timed out"
    except Exception as e:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return None, None, f"Clone failed: {e}"


async def clone_with_github_fallback(
    repo_url: str, return_to: str
) -> tuple[AppManifest | None, str | None, str | None, str | None]:
    """Clone a repo, retrying with a stored GitHub OAuth token if needed.

    Returns (manifest, clone_dir, error, authorize_url).
    """
    manifest, tmp_dir, error = await clone_and_read_manifest(repo_url)

    if error and "github.com" in repo_url:
        try:
            token = await get_oauth_token("github", ["repo"], return_to=return_to)
        except ServiceNotAvailable as e:
            return None, None, e.message, None
        except OAuthAuthorizationRequired as e:
            return None, None, error, e.authorize_url
        manifest, tmp_dir, error = await clone_and_read_manifest(repo_url, github_token=token)

    return manifest, tmp_dir, error, None


def validate_manifest(manifest: AppManifest, db: sqlite3.Connection, app_name: str | None = None) -> str | None:
    """Check reserved names and duplicates. Returns error string or None."""
    if app_name is None:
        app_name = manifest.name

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", app_name):
        return "App name must be lowercase alphanumeric (hyphens allowed, not at start/end)"

    if f"/{app_name}" in RESERVED_PATHS:
        return f"App name '{app_name}' conflicts with a reserved path"

    existing = db.execute("SELECT name FROM apps WHERE name = ?", (app_name,)).fetchone()
    if existing:
        return f"App name already in use by '{existing['name']}'"

    return None


def insert_and_deploy(
    manifest: AppManifest,
    repo_path: str,
    config: Config,
    db: sqlite3.Connection,
    grant_permissions: set[str],
    app_name: str | None = None,
    repo_url: str | None = None,
    port_overrides: dict[str, int] | None = None,
) -> str:
    """Insert app into DB and start background deploy.

    Returns app_name. Raises RuntimeError if no port available or
    storage limit is exceeded.

    port_overrides: optional dict of label -> host_port from CLI/API.
    """
    if app_name is None:
        app_name = manifest.name

    storage.check_before_deploy(config)
    local_port = allocate_port(config.port_range_start, config.port_range_end)

    # Apply port overrides from caller (CLI --port flags, etc.)
    mappings = [
        attr.evolve(pm, host_port=port_overrides.get(pm.label, pm.host_port)) if port_overrides else pm
        for pm in manifest.port_mappings
    ]

    # Resolve auto-assigned ports (host_port=0)
    resolved_mappings = resolve_port_mappings(mappings, db, config.port_range_start, config.port_range_end)

    env_vars = provision_data(
        app_name,
        manifest,
        config.persistent_data_dir,
        config.temporary_data_dir,
        port=config.port,
        zone_domain=config.zone_domain,
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
    )

    cursor = db.execute(
        """INSERT INTO apps
           (name, manifest_name, version, description, runtime_type, repo_path, repo_url,
            health_check, local_port, container_port, memory_mb, cpu_millicores,
            gpu, public_paths, manifest_raw, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            app_name,
            manifest.name,
            manifest.version,
            manifest.description,
            manifest.runtime_type,
            repo_path,
            repo_url,
            manifest.health_check,
            local_port,
            manifest.container_port,
            manifest.memory_mb,
            manifest.cpu_millicores,
            int(manifest.gpu),
            json.dumps(manifest.public_paths),
            manifest.raw_toml,
            "building",
        ),
    )
    # Allocate this app's subuid/subgid window now that we have its id.
    # The mapping is sticky across rebuilds/restarts so on-disk file
    # ownership stays consistent.
    app_id = cursor.lastrowid
    assert app_id is not None, "SQLite should always populate lastrowid on INSERT"
    db.execute(
        "UPDATE apps SET uid_map_base = ? WHERE id = ?",
        (compute_uid_map_base(app_id), app_id),
    )

    # Store resolved port mappings
    for pm in resolved_mappings:
        db.execute(
            "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES (?, ?, ?, ?)",
            (app_name, pm.label, pm.container_port, pm.host_port),
        )

    for db_name in manifest.sqlite_dbs:
        db_path = env_vars.get(f"OPENHOST_SQLITE_{db_name.upper()}", "")
        db.execute(
            "INSERT INTO app_databases (app_name, db_name, db_path) VALUES (?, ?, ?)",
            (app_name, db_name, db_path),
        )
    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        db.execute(
            "INSERT OR REPLACE INTO app_tokens (app_name, token) VALUES (?, ?)",
            (app_name, app_token),
        )

    for svc_name in manifest.provides_services:
        db.execute(
            "INSERT OR REPLACE INTO service_providers (service_name, app_name) VALUES (?, ?)",
            (svc_name, app_name),
        )

    db.commit()

    # Grant only the service permissions the caller explicitly approved.
    # Done after commit so the app row exists for the FK constraint.
    all_manifest_keys: set[str] = set()
    for svc_name, keys in manifest.requires_services.items():
        for key_spec in keys:
            perm_key = f"{svc_name}/{key_spec['key']}"
            all_manifest_keys.add(perm_key)
            if perm_key not in grant_permissions and key_spec.get("required", True):
                logger.warning("App %s deployed without required permission %s", app_name, perm_key)
        permission_keys = [k for k in (f"{svc_name}/{key_spec['key']}" for key_spec in keys) if k in grant_permissions]
        if permission_keys:
            grant_permissions_fn(app_name, permission_keys)

    unknown = grant_permissions - all_manifest_keys
    if unknown:
        logger.warning("App %s granted unknown permissions not in manifest: %s", app_name, unknown)

    threading.Thread(
        target=deploy_app_background,
        args=(manifest, repo_path, local_port, env_vars, config),
        kwargs={"app_name": app_name, "port_mappings": resolved_mappings},
        daemon=True,
    ).start()

    return app_name


def _resolve_uid_map_base(db: sqlite3.Connection, app_name: str) -> int:
    """Return the per-app subuid base, allocating on first use if needed.

    A stored value of 0 is the schema's "not yet assigned" sentinel (rows
    inserted before the column existed, or rows whose id fell outside the
    pool at migration time).  We compute the real base via the
    deterministic formula and persist it so every subsequent start reuses
    the same host UID window, keeping on-disk file ownership stable.

    Raises ``RuntimeError`` if the app isn't in the database, and propagates
    ``ValueError`` from ``compute_uid_map_base`` for ids past the pool.
    """
    row = db.execute(
        "SELECT id, uid_map_base FROM apps WHERE name = ?",
        (app_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"App {app_name!r} not found in database")
    base = row["uid_map_base"]
    if base:
        return int(base)
    base = compute_uid_map_base(row["id"])
    db.execute(
        "UPDATE apps SET uid_map_base = ? WHERE name = ?",
        (base, app_name),
    )
    db.commit()
    return base


def deploy_app_background(
    manifest: AppManifest,
    repo_path: str,
    local_port: int,
    env_vars: dict[str, str],
    config: Config,
    app_name: str | None = None,
    port_mappings: list[PortMapping] | None = None,
) -> None:
    """Build and start an app in a background thread."""
    if app_name is None:
        app_name = manifest.name

    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        storage.check_before_deploy(config)

        # Retry container builds for transient failures (network blip during
        # base-image pull, temporary lock contention, etc.).
        max_build_attempts = 3
        image_tag = ""
        for attempt in range(1, max_build_attempts + 1):
            try:
                image_tag = build_image(
                    app_name,
                    repo_path,
                    manifest.container_image,
                    temp_data_dir=config.temporary_data_dir,
                )
                break
            except RuntimeError:
                if attempt == max_build_attempts:
                    raise
                logger.warning(
                    "Container build attempt %d/%d for %s failed, retrying in %ds",
                    attempt,
                    max_build_attempts,
                    app_name,
                    attempt * 5,
                )
                time.sleep(attempt * 5)
        db.execute(
            "UPDATE apps SET status = 'starting' WHERE name = ?",
            (app_name,),
        )
        db.commit()
        uid_map_base = _resolve_uid_map_base(db, app_name)
        container_id = run_container(
            app_name,
            image_tag,
            manifest,
            local_port,
            env_vars,
            config.persistent_data_dir,
            config.temporary_data_dir,
            uid_map_base=uid_map_base,
            port_mappings=port_mappings,
        )
        db.execute(
            "UPDATE apps SET container_id = ? WHERE name = ?",
            (container_id, app_name),
        )
        db.commit()

        if wait_for_ready(local_port):
            db.execute(
                "UPDATE apps SET status = 'running' WHERE name = ?",
                (app_name,),
            )
        else:
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                ("App started but not responding to HTTP", app_name),
            )
        db.commit()
    except Exception as e:
        logger.exception("Failed to deploy %s", app_name)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
            (str(e), app_name),
        )
        db.commit()
    finally:
        db.close()


def wait_for_ready(local_port: int, timeout: int = 60) -> bool:
    """Poll the app's local port until it responds to HTTP. Returns True if ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{local_port}/", timeout=2)
            if r.status_code < 500:
                return True
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            ConnectionResetError,
        ):
            pass
        time.sleep(1)
    return False


def _load_port_mappings_from_db(app_name: str, db: sqlite3.Connection) -> list[PortMapping]:
    """Load resolved port mappings from the database."""
    rows = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_name = ?",
        (app_name,),
    ).fetchall()
    return [PortMapping(label=r["label"], container_port=r["container_port"], host_port=r["host_port"]) for r in rows]


def _sync_port_mappings(
    app_name: str,
    new_mappings: list[PortMapping],
    db: sqlite3.Connection,
    config: Config,
) -> None:
    """Sync port mappings on reload: preserve existing host_port, handle adds/removes."""
    existing = {
        r["label"]: r
        for r in db.execute(
            "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_name = ?",
            (app_name,),
        ).fetchall()
    }

    new_labels = {pm.label for pm in new_mappings}
    old_labels = set(existing.keys())

    # Remove labels no longer in manifest
    for removed in old_labels - new_labels:
        db.execute(
            "DELETE FROM app_port_mappings WHERE app_name = ? AND label = ?",
            (app_name, removed),
        )

    # Build list for resolve: preserve existing host_port, new ones get 0 (auto)
    to_resolve: list[PortMapping] = []
    for pm in new_mappings:
        if pm.label in existing:
            to_resolve.append(attr.evolve(pm, host_port=existing[pm.label]["host_port"]))
        else:
            to_resolve.append(pm)

    resolved = resolve_port_mappings(
        to_resolve, db, config.port_range_start, config.port_range_end, exclude_app=app_name
    )

    # Upsert resolved mappings
    for pm in resolved:
        if pm.label in existing:
            db.execute(
                "UPDATE app_port_mappings SET container_port = ?, host_port = ? WHERE app_name = ? AND label = ?",
                (pm.container_port, pm.host_port, app_name, pm.label),
            )
        else:
            db.execute(
                "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES (?, ?, ?, ?)",
                (app_name, pm.label, pm.container_port, pm.host_port),
            )


def start_app_process(app_name: str, db: sqlite3.Connection, config: Config) -> None:
    """Start the process for an app. Updates DB with status and container id."""
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    storage.check_before_deploy(config)

    manifest = parse_manifest(app_row["repo_path"])
    env_vars = provision_data(
        app_row["name"],
        manifest,
        config.persistent_data_dir,
        config.temporary_data_dir,
        port=config.port,
        zone_domain=config.zone_domain,
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
    )

    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        db.execute(
            "INSERT OR REPLACE INTO app_tokens (app_name, token) VALUES (?, ?)",
            (app_name, app_token),
        )

    db.execute("DELETE FROM service_providers WHERE app_name = ?", (app_name,))
    for svc_name in manifest.provides_services:
        db.execute(
            "INSERT OR REPLACE INTO service_providers (service_name, app_name) VALUES (?, ?)",
            (svc_name, app_name),
        )

    # Load resolved port mappings from DB (preserves host_port assignments)
    port_mappings = _load_port_mappings_from_db(app_name, db)

    db.execute(
        "UPDATE apps SET status = 'starting', error_message = NULL WHERE name = ?",
        (app_name,),
    )
    db.commit()

    image_tag = build_image(
        app_row["name"],
        app_row["repo_path"],
        manifest.container_image,
        temp_data_dir=config.temporary_data_dir,
    )
    uid_map_base = _resolve_uid_map_base(db, app_name)
    container_id = run_container(
        app_row["name"],
        image_tag,
        manifest,
        app_row["local_port"],
        env_vars,
        config.persistent_data_dir,
        config.temporary_data_dir,
        uid_map_base=uid_map_base,
        port_mappings=port_mappings,
    )
    db.execute(
        "UPDATE apps SET container_id = ? WHERE name = ?",
        (container_id, app_name),
    )
    db.commit()

    if wait_for_ready(app_row["local_port"]):
        db.execute(
            "UPDATE apps SET status = 'running' WHERE name = ?",
            (app_name,),
        )
    else:
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
            ("App started but not responding to HTTP", app_name),
        )
    db.commit()


def app_log_path(app_name: str, config: Config) -> str:
    """Return the build/deploy log file path for an app.

    Thin adapter over ``build_log_path`` in ``containers.py`` so callers
    that have a Config can avoid plumbing temporary_data_dir through by
    hand.  The single source of truth for the path format lives in
    ``containers.build_log_path``.
    """
    return build_log_path(app_name, config.temporary_data_dir)


def git_pull(
    repo_path: str,
    app_name: str,
    github_token: str | None = None,
    log_file: str | None = None,
    repo_url: str | None = None,
) -> tuple[bool, str | None]:
    """Try to git pull. Returns (ok, error_msg)."""

    def _log(msg: str) -> None:
        logger.info("%s: %s", app_name, msg)
        if log_file:
            with open(log_file, "a") as f:
                f.write(msg + "\n")

    if repo_url:
        base_url, _ref = parse_repo_url(repo_url)
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", base_url],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            _log(f"failed to set remote url: {e}")
            return False, str(e)

    original_url = None
    if github_token:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            original_url = result.stdout.strip()
            authed_url = inject_github_token_in_url(original_url, github_token)
            if authed_url != original_url:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", authed_url],
                    cwd=repo_path,
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            pass

    try:
        _log("$ git pull")
        result = subprocess.run(
            ["git", "pull"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.stdout.strip():
            _log(result.stdout.strip())
        if result.stderr.strip():
            _log(result.stderr.strip())
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "git pull failed"
            return False, error_msg
        return True, None
    except Exception as e:
        _log(f"git pull failed: {e}")
        return False, str(e)
    finally:
        if github_token and original_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", original_url],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            )


def reload_app_background(app_name: str, repo_path: str, config: Config) -> None:
    """Reload an app in a background thread: re-read manifest, rebuild, start."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        if repo_path and os.path.isdir(os.path.join(repo_path, ".git")):
            pass  # git pull already done before background thread
        else:
            source_dir = os.path.join(config.apps_dir, app_name)
            if not os.path.isdir(source_dir):
                source_dir = None  # type: ignore[assignment]
                for entry in os.listdir(config.apps_dir):
                    candidate = os.path.join(config.apps_dir, entry)
                    if os.path.isfile(os.path.join(candidate, "openhost.toml")):
                        try:
                            m = parse_manifest(candidate)
                            if m.name == app_name:
                                source_dir = candidate
                                break
                        except ValueError:
                            pass
            if source_dir and os.path.isdir(source_dir):
                if os.path.exists(repo_path):
                    shutil.rmtree(repo_path)
                shutil.copytree(source_dir, repo_path)
                logger.info("Re-copied %s from %s", app_name, source_dir)

        if repo_path and os.path.isdir(repo_path):
            try:
                manifest = parse_manifest(repo_path)
                db.execute(
                    "UPDATE apps SET public_paths = ?, manifest_raw = ?, manifest_name = ? WHERE name = ?",
                    (
                        json.dumps(manifest.public_paths),
                        manifest.raw_toml,
                        manifest.name,
                        app_name,
                    ),
                )

                # Diff port mappings: preserve existing host_port for unchanged labels
                _sync_port_mappings(app_name, manifest.port_mappings, db, config)

                db.commit()
            except ValueError:
                pass

        start_app_process(app_name, db, config)
    except Exception as e:
        logger.exception("Failed to reload %s", app_name)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
            (str(e), app_name),
        )
        db.commit()
    finally:
        db.close()
