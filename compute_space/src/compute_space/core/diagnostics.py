"""Collect diagnostic information for debugging OpenHost instances and apps.

Two public entry points:

  - :func:`collect_platform_diagnostics` — a snapshot of the whole instance:
    OpenHost git checkout (branch/SHA/dirty), host OS/kernel, Python and
    installed dependency versions, container runtime (podman) info, disk usage,
    and a summary of every installed app.  Owner-only; intended to be copied or
    downloaded and pasted into a bug report.

  - :func:`collect_app_diagnostics` — a per-app snapshot: the app's declared
    version + manifest git checkout, container status, plus a slim slice of the
    same host/system info so an app report is self-contained.

Every collector is defensive: a failure gathering one field degrades that field
to ``None``/an error string rather than failing the whole report, because a
diagnostics bundle is most valuable precisely when the instance is unhealthy.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path

import attr

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import get_remote_url
from compute_space.core.git_ops import is_dirty
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.storage import storage_status

# The schema version of the diagnostics payload.  Bump when the shape changes
# so consumers (support tooling, the CLI, the dashboard) can detect an
# incompatible bundle.  Present on both the platform and per-app bundles.
DIAGNOSTICS_SCHEMA_VERSION = 1

# Distribution names whose versions are worth surfacing in a bug report.  These
# are the packages most likely to explain a runtime bug; the full environment is
# large and noisy, so we curate rather than dump everything.
_KEY_DEPENDENCIES = (
    "litestar",
    "hypercorn",
    "GitPython",
    "attrs",
    "cattrs",
    "typed-settings",
    "httpx",
    "bcrypt",
    "jinja2",
    "tomli-w",
    "cappa",
)

_SUBPROCESS_TIMEOUT_S = 10


# ─── attrs models ────────────────────────────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class GitInfo:
    """Git checkout state for a repository on disk.

    ``sha`` is empty and ``branch`` is None when the path isn't a git checkout
    (e.g. builtin apps or tarball deploys). ``branch`` is None when HEAD is
    detached even if ``sha`` is populated.
    """

    branch: str | None
    sha: str
    short_sha: str
    dirty: bool
    remote_url: str | None


@attr.s(auto_attribs=True, frozen=True)
class SystemInfo:
    """Host OS / Python / process facts."""

    hostname: str
    platform: str
    system: str
    release: str
    machine: str
    processor: str
    python_version: str
    python_implementation: str
    cpu_count: int | None
    boot_time: str | None


@attr.s(auto_attribs=True, frozen=True)
class ContainerRuntimeInfo:
    """Facts about the container runtime (podman)."""

    available: bool
    version: str | None
    rootless: bool | None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppDiagnosticsSummary:
    """Per-app entry in the platform diagnostics bundle."""

    app_id: str
    name: str
    status: str
    version: str | None
    runtime_type: str | None
    error_message: str | None
    git: GitInfo | None


@attr.s(auto_attribs=True, frozen=True)
class PlatformDiagnostics:
    """Full instance diagnostics bundle (owner-only)."""

    schema_version: int
    generated_at: str
    zone_domain: str
    openhost: GitInfo
    system: SystemInfo
    container_runtime: ContainerRuntimeInfo
    dependencies: dict[str, str]
    storage: dict[str, object]
    apps: list[AppDiagnosticsSummary]


@attr.s(auto_attribs=True, frozen=True)
class AppDiagnostics:
    """Per-app diagnostics bundle (owner-only).

    Includes a slice of host/system info so an app report is self-contained and
    useful on its own, without the caller also having to grab the platform
    bundle.
    """

    schema_version: int
    generated_at: str
    zone_domain: str
    app_id: str
    name: str
    status: str
    version: str | None
    runtime_type: str | None
    error_message: str | None
    container_id: str | None
    git: GitInfo | None
    system: SystemInfo
    container_runtime: ContainerRuntimeInfo
    openhost: GitInfo


# ─── low-level collectors ──────────────────────────────────────────────────


async def _collect_git_info(repo_path: Path | None) -> GitInfo | None:
    """Return :class:`GitInfo` for ``repo_path`` or None when it has no .git.

    Never raises: any error while reading git state degrades to None so a
    single bad repo doesn't sink the whole bundle.
    """
    if repo_path is None:
        return None
    if not (repo_path / ".git").exists():
        return None
    try:
        branch = await get_branch_name(repo_path)
        sha = await get_head_sha(repo_path)
        dirty = await is_dirty(repo_path)
    except Exception:
        logger.opt(exception=True).warning("Failed to read git info for %s", repo_path)
        return None
    remote_url: str | None
    try:
        remote_url = await get_remote_url(repo_path)
    except Exception:
        # Missing/unreadable remote is common (no 'origin'); not worth a warning.
        remote_url = None
    return GitInfo(branch=branch, sha=sha, short_sha=sha[:8], dirty=dirty, remote_url=remote_url)


def _collect_system_info() -> SystemInfo:
    """Gather host OS / Python facts. Best-effort; missing fields become None."""
    uname = platform.uname()
    try:
        cpu_count = os.cpu_count()
    except Exception:
        cpu_count = None
    boot_time = _read_boot_time()
    return SystemInfo(
        hostname=uname.node,
        platform=platform.platform(),
        system=uname.system,
        release=uname.release,
        machine=uname.machine,
        processor=uname.processor,
        python_version=platform.python_version(),
        python_implementation=platform.python_implementation(),
        cpu_count=cpu_count,
        boot_time=boot_time,
    )


def _read_boot_time() -> str | None:
    """Return system boot time as an ISO-8601 UTC string, or None if unavailable.

    Reads /proc/stat's ``btime`` (Linux only); avoids adding a psutil dependency.
    """
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    epoch = int(line.split()[1])
                    return datetime.fromtimestamp(epoch, UTC).isoformat()
    except Exception:
        return None
    return None


def _collect_dependencies() -> dict[str, str]:
    """Return {distribution_name: version} for the curated key dependencies.

    A dependency that isn't installed maps to ``"(not installed)"`` rather than
    being omitted, so a missing package is visible in the report.
    """
    versions: dict[str, str] = {}
    for name in _KEY_DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "(not installed)"
        except Exception:
            versions[name] = "(error)"
    return versions


def _collect_container_runtime() -> ContainerRuntimeInfo:
    """Probe podman for version + rootless status.

    Mirrors the ``openhost doctor`` probe (parse ``podman info --format json``,
    assert the rootless flag) so the two agree on what "healthy" looks like.
    """
    if shutil.which("podman") is None:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman not found on PATH")
    try:
        info_proc = subprocess.run(
            ["podman", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman info timed out")
    except Exception as e:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error=str(e))

    if info_proc.returncode != 0:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman info failed")

    version: str | None = None
    rootless: bool | None = None
    try:
        info = json.loads(info_proc.stdout)
        host = info.get("host", {})
        # podman reports its own version under the top-level ``version`` table
        # (``version.Version``); ``host.serverVersion`` is not populated in
        # current releases, so read the former and fall back to the latter.
        version = info.get("version", {}).get("Version") or host.get("serverVersion")
        rootless_val = host.get("security", {}).get("rootless")
        rootless = rootless_val if isinstance(rootless_val, bool) else None
    except (json.JSONDecodeError, AttributeError):
        return ContainerRuntimeInfo(
            available=True, version=None, rootless=None, error="podman info returned non-JSON output"
        )
    return ContainerRuntimeInfo(available=True, version=version, rootless=rootless, error=None)


def _manifest_fields(manifest_raw: str | None) -> tuple[str | None, str | None]:
    """Parse (version, runtime_type) from a stored manifest, or (None, None).

    Re-parsing ``manifest_raw`` is more accurate than the ``apps.version``
    column, which is only written at install time and not on reload.
    """
    if not manifest_raw:
        return None, None
    try:
        manifest = parse_manifest_from_string(manifest_raw)
    except Exception:
        return None, None
    return manifest.version, manifest.runtime_type


# ─── platform diagnostics ────────────────────────────────────────────────────


async def _collect_app_summary(row: sqlite3.Row) -> AppDiagnosticsSummary:
    version, runtime_type = _manifest_fields(row["manifest_raw"])
    # Fall back to the stored column when the manifest can't be re-parsed.
    if version is None:
        try:
            version = row["version"]
        except (IndexError, KeyError):
            version = None
    repo_path = row["repo_path"]
    git = await _collect_git_info(Path(repo_path) if repo_path else None)
    return AppDiagnosticsSummary(
        app_id=row["app_id"],
        name=row["name"],
        status=row["status"],
        version=version,
        runtime_type=runtime_type,
        error_message=row["error_message"],
        git=git,
    )


async def collect_platform_diagnostics(db: sqlite3.Connection, config: Config) -> PlatformDiagnostics:
    """Assemble the full instance diagnostics bundle."""
    openhost_git = await _collect_git_info(OPENHOST_PROJECT_DIR)
    if openhost_git is None:
        # OPENHOST_PROJECT_DIR isn't a git checkout (tarball deploy): still
        # emit a GitInfo so the field shape is stable for consumers.
        openhost_git = GitInfo(branch=None, sha="", short_sha="", dirty=False, remote_url=None)

    try:
        storage = storage_status(config)
    except Exception:
        logger.opt(exception=True).warning("Failed to collect storage status for diagnostics")
        storage = {}

    apps: list[AppDiagnosticsSummary] = []
    try:
        rows = db.execute(
            "SELECT app_id, name, status, version, runtime_type, error_message, repo_path, manifest_raw "
            "FROM apps ORDER BY name"
        ).fetchall()
        for row in rows:
            apps.append(await _collect_app_summary(row))
    except Exception:
        logger.opt(exception=True).warning("Failed to collect per-app diagnostics summary")

    return PlatformDiagnostics(
        schema_version=DIAGNOSTICS_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        zone_domain=config.zone_domain,
        openhost=openhost_git,
        system=_collect_system_info(),
        container_runtime=_collect_container_runtime(),
        dependencies=_collect_dependencies(),
        storage=storage,
        apps=apps,
    )


async def collect_app_diagnostics(row: sqlite3.Row, config: Config) -> AppDiagnostics:
    """Assemble a per-app diagnostics bundle for the given ``apps`` row."""
    version, runtime_type = _manifest_fields(row["manifest_raw"])
    if version is None:
        try:
            version = row["version"]
        except (IndexError, KeyError):
            version = None

    repo_path = row["repo_path"]
    git = await _collect_git_info(Path(repo_path) if repo_path else None)

    openhost_git = await _collect_git_info(OPENHOST_PROJECT_DIR)
    if openhost_git is None:
        openhost_git = GitInfo(branch=None, sha="", short_sha="", dirty=False, remote_url=None)

    return AppDiagnostics(
        schema_version=DIAGNOSTICS_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        zone_domain=config.zone_domain,
        app_id=row["app_id"],
        name=row["name"],
        status=row["status"],
        version=version,
        runtime_type=runtime_type,
        error_message=row["error_message"],
        container_id=row["container_id"],
        git=git,
        system=_collect_system_info(),
        container_runtime=_collect_container_runtime(),
        openhost=openhost_git,
    )
