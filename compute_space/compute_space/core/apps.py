"""App lifecycle operations: clone, deploy, start, stop, reload, validate.

Extracted from routes/apps.py — no HTTP/Quart dependencies.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

import httpx
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

import compute_space.core.storage as storage
from compute_space.config import Config
from compute_space.core.containers import build_image
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
from compute_space.db import get_session
from compute_space.db import get_session_maker
from compute_space.db.models import App
from compute_space.db.models import AppDatabase
from compute_space.db.models import AppPortMapping
from compute_space.db.models import AppToken
from compute_space.db.models import ServiceProvider

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


async def validate_manifest(manifest: AppManifest, session: AsyncSession, app_name: str | None = None) -> str | None:
    """Check reserved names and duplicates. Returns error string or None."""
    if app_name is None:
        app_name = manifest.name

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", app_name):
        return "App name must be lowercase alphanumeric (hyphens allowed, not at start/end)"

    if f"/{app_name}" in RESERVED_PATHS:
        return f"App name '{app_name}' conflicts with a reserved path"

    existing = (await session.execute(select(App.name).where(App.name == app_name))).scalar_one_or_none()
    if existing:
        return f"App name already in use by '{existing}'"

    return None


async def insert_and_deploy(
    manifest: AppManifest,
    repo_path: str,
    config: Config,
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
    session = get_session()
    local_port = await allocate_port(session, config.port_range_start, config.port_range_end)

    # Apply port overrides from caller (CLI --port flags, etc.)
    mappings = [
        dataclasses.replace(pm, host_port=port_overrides.get(pm.label, pm.host_port)) if port_overrides else pm
        for pm in manifest.port_mappings
    ]

    # Resolve auto-assigned ports (host_port=0)
    resolved_mappings = await resolve_port_mappings(mappings, session, config.port_range_start, config.port_range_end)

    env_vars = provision_data(
        app_name,
        manifest,
        config.persistent_data_dir,
        config.temporary_data_dir,
        port=config.port,
        zone_domain=config.zone_domain,
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
    )

    session.add(
        App(
            name=app_name,
            manifest_name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            runtime_type=manifest.runtime_type,
            repo_path=repo_path,
            repo_url=repo_url,
            health_check=manifest.health_check,
            local_port=local_port,
            container_port=manifest.container_port,
            memory_mb=manifest.memory_mb,
            cpu_millicores=manifest.cpu_millicores,
            gpu=int(manifest.gpu),
            public_paths=json.dumps(manifest.public_paths),
            manifest_raw=manifest.raw_toml,
            status="building",
        )
    )

    # Store resolved port mappings
    for pm in resolved_mappings:
        session.add(
            AppPortMapping(
                app_name=app_name,
                label=pm.label,
                container_port=pm.container_port,
                host_port=pm.host_port,
            )
        )

    for db_name in manifest.sqlite_dbs:
        db_path = env_vars.get(f"OPENHOST_SQLITE_{db_name.upper()}", "")
        session.add(AppDatabase(app_name=app_name, db_name=db_name, db_path=db_path))

    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        app_token_hash = hashlib.sha256(app_token.encode()).hexdigest()
        stmt = sqlite_insert(AppToken).values(app_name=app_name, token_hash=app_token_hash)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["app_name"], set_={"token_hash": app_token_hash})
        )

    for svc_name in manifest.provides_services:
        svc_stmt = sqlite_insert(ServiceProvider).values(service_name=svc_name, app_name=app_name)
        await session.execute(svc_stmt.on_conflict_do_nothing(index_elements=["service_name", "app_name"]))

    await session.commit()

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
            await grant_permissions_fn(app_name, permission_keys)

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


def deploy_app_background(
    manifest: AppManifest,
    repo_path: str,
    local_port: int,
    env_vars: dict[str, str],
    config: Config,
    app_name: str | None = None,
    port_mappings: list[PortMapping] | None = None,
) -> None:
    """Build and start an app in a background thread (sync entry point)."""
    asyncio.run(
        _deploy_app_background_async(
            manifest, repo_path, local_port, env_vars, config, app_name=app_name, port_mappings=port_mappings
        )
    )


_UNSET_ERR = "__unset_err__"


async def _set_app_status(
    session: AsyncSession, app_name: str, status: str, error_message: str | None | str = _UNSET_ERR
) -> None:
    values: dict[str, object] = {"status": status}
    if error_message != _UNSET_ERR:
        values["error_message"] = error_message
    await session.execute(update(App).where(App.name == app_name).values(**values))
    await session.commit()


async def _deploy_app_background_async(
    manifest: AppManifest,
    repo_path: str,
    local_port: int,
    env_vars: dict[str, str],
    config: Config,
    app_name: str | None = None,
    port_mappings: list[PortMapping] | None = None,
) -> None:
    if app_name is None:
        app_name = manifest.name

    async with get_session_maker()() as session:
        try:
            storage.check_before_deploy(config)

            # Retry Docker builds for transient failures (daemon not ready yet,
            # network blip during image pull, etc.).
            max_build_attempts = 3
            image_tag = ""
            for attempt in range(1, max_build_attempts + 1):
                try:
                    image_tag = await asyncio.to_thread(
                        build_image,
                        app_name,
                        repo_path,
                        manifest.container_image,
                        config.temporary_data_dir,
                    )
                    break
                except RuntimeError:
                    if attempt == max_build_attempts:
                        raise
                    logger.warning(
                        "Docker build attempt %d/%d for %s failed, retrying in %ds",
                        attempt,
                        max_build_attempts,
                        app_name,
                        attempt * 5,
                    )
                    await asyncio.sleep(attempt * 5)

            await _set_app_status(session, app_name, "starting")
            container_id = await asyncio.to_thread(
                run_container,
                app_name,
                image_tag,
                manifest,
                local_port,
                env_vars,
                config.persistent_data_dir,
                config.temporary_data_dir,
                port_mappings,
            )
            await session.execute(update(App).where(App.name == app_name).values(docker_container_id=container_id))
            await session.commit()

            if await asyncio.to_thread(wait_for_ready, local_port):
                await _set_app_status(session, app_name, "running")
            else:
                await _set_app_status(session, app_name, "error", "App started but not responding to HTTP")
        except Exception as e:
            logger.exception("Failed to deploy %s", app_name)
            await _set_app_status(session, app_name, "error", str(e))


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


async def _load_port_mappings_from_db(app_name: str, session: AsyncSession) -> list[PortMapping]:
    """Load resolved port mappings from the database."""
    rows = (
        await session.execute(
            select(AppPortMapping.label, AppPortMapping.container_port, AppPortMapping.host_port).where(
                AppPortMapping.app_name == app_name
            )
        )
    ).all()
    return [PortMapping(label=r.label, container_port=r.container_port, host_port=r.host_port) for r in rows]


async def _sync_port_mappings(
    app_name: str,
    new_mappings: list[PortMapping],
    config: Config,
) -> None:
    """Sync port mappings on reload: preserve existing host_port, handle adds/removes."""
    async with get_session_maker()() as session, session.begin():
        existing_rows = (
            await session.execute(
                select(AppPortMapping.label, AppPortMapping.container_port, AppPortMapping.host_port).where(
                    AppPortMapping.app_name == app_name
                )
            )
        ).all()
        existing = {r.label: {"container_port": r.container_port, "host_port": r.host_port} for r in existing_rows}

        new_labels = {pm.label for pm in new_mappings}
        old_labels = set(existing.keys())

        # Remove labels no longer in manifest
        for removed in old_labels - new_labels:
            await session.execute(
                delete(AppPortMapping).where(AppPortMapping.app_name == app_name, AppPortMapping.label == removed)
            )

        # Build list for resolve: preserve existing host_port, new ones get 0 (auto)
        to_resolve: list[PortMapping] = []
        for pm in new_mappings:
            if pm.label in existing:
                to_resolve.append(dataclasses.replace(pm, host_port=existing[pm.label]["host_port"]))
            else:
                to_resolve.append(pm)

        resolved = await resolve_port_mappings(
            to_resolve, session, config.port_range_start, config.port_range_end, exclude_app=app_name
        )

        # Upsert resolved mappings
        for pm in resolved:
            if pm.label in existing:
                await session.execute(
                    update(AppPortMapping)
                    .where(AppPortMapping.app_name == app_name, AppPortMapping.label == pm.label)
                    .values(container_port=pm.container_port, host_port=pm.host_port)
                )
            else:
                session.add(
                    AppPortMapping(
                        app_name=app_name,
                        label=pm.label,
                        container_port=pm.container_port,
                        host_port=pm.host_port,
                    )
                )


async def start_app_process(app_name: str, session: AsyncSession, config: Config) -> None:
    """Start the process for an app. Updates DB with status and container id."""
    app_row = (await session.execute(select(App).where(App.name == app_name))).scalar_one()
    storage.check_before_deploy(config)

    manifest = parse_manifest(app_row.repo_path)
    env_vars = provision_data(
        app_row.name,
        manifest,
        config.persistent_data_dir,
        config.temporary_data_dir,
        port=config.port,
        zone_domain=config.zone_domain,
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
    )

    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        app_token_hash = hashlib.sha256(app_token.encode()).hexdigest()
        stmt = sqlite_insert(AppToken).values(app_name=app_name, token_hash=app_token_hash)
        await session.execute(
            stmt.on_conflict_do_update(index_elements=["app_name"], set_={"token_hash": app_token_hash})
        )

    await session.execute(delete(ServiceProvider).where(ServiceProvider.app_name == app_name))
    for svc_name in manifest.provides_services:
        svc_stmt = sqlite_insert(ServiceProvider).values(service_name=svc_name, app_name=app_name)
        await session.execute(svc_stmt.on_conflict_do_nothing(index_elements=["service_name", "app_name"]))

    # Load resolved port mappings from DB (preserves host_port assignments)
    port_mappings = await _load_port_mappings_from_db(app_name, session)

    await session.execute(update(App).where(App.name == app_name).values(status="starting", error_message=None))
    await session.commit()

    image_tag = await asyncio.to_thread(
        build_image,
        app_row.name,
        app_row.repo_path,
        manifest.container_image,
        config.temporary_data_dir,
    )
    container_id = await asyncio.to_thread(
        run_container,
        app_row.name,
        image_tag,
        manifest,
        app_row.local_port,
        env_vars,
        config.persistent_data_dir,
        config.temporary_data_dir,
        port_mappings,
    )
    await session.execute(update(App).where(App.name == app_name).values(docker_container_id=container_id))
    await session.commit()

    if await asyncio.to_thread(wait_for_ready, app_row.local_port):
        await _set_app_status(session, app_name, "running")
    else:
        await _set_app_status(session, app_name, "error", "App started but not responding to HTTP")


def app_log_path(app_name: str, config: Config) -> str:
    """Return the log file path for an app."""
    return os.path.join(config.temporary_data_dir, "app_temp_data", app_name, "docker.log")


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
    """Reload an app in a background thread (sync entry point)."""
    asyncio.run(_reload_app_background_async(app_name, repo_path, config))


async def _reload_app_background_async(app_name: str, repo_path: str, config: Config) -> None:
    async with get_session_maker()() as session:
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
                    await session.execute(
                        update(App)
                        .where(App.name == app_name)
                        .values(
                            public_paths=json.dumps(manifest.public_paths),
                            manifest_raw=manifest.raw_toml,
                            manifest_name=manifest.name,
                        )
                    )
                    await session.commit()

                    # Diff port mappings: preserve existing host_port for unchanged labels
                    # (opens its own session internally)
                    await _sync_port_mappings(app_name, manifest.port_mappings, config)
                except ValueError:
                    pass

            await start_app_process(app_name, session, config)
        except Exception as e:
            logger.exception("Failed to reload %s", app_name)
            await _set_app_status(session, app_name, "error", str(e))
