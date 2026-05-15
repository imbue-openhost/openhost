from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import git
from loguru import logger

# __init__.py lives at openhost_system_agent/src/openhost_system_agent/__init__.py
# Walk up to the repo root: __init__.py -> openhost_system_agent -> src -> openhost_system_agent -> <repo root>
_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent.parent.parent


def _repo() -> git.Repo:
    return git.Repo(_PROJECT_DIR)


def _get_remote(repo: git.Repo) -> git.Remote:
    try:
        return repo.remote("origin")
    except (AttributeError, ValueError) as e:
        raise RuntimeError("remote 'origin' is not set") from e


def _branch_name(repo: git.Repo) -> str:
    try:
        return repo.active_branch.name
    except TypeError:
        return repo.head.commit.hexsha[:8]


_KNOWN_SCHEMES = {"http", "https", "ssh", "git", "file"}


def _strip_credentials(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port = f"{host_port}:{parsed.port}"
        return parsed._replace(netloc=host_port).geturl()
    return url


def fetch_updates() -> dict[str, object]:
    repo = _repo()
    remote = _get_remote(repo)
    remote.fetch()

    if repo.is_dirty(untracked_files=True):
        return {"state": "DIRTY"}

    try:
        branch = repo.active_branch
    except TypeError:
        return {"state": "UP_TO_DATE"}

    tracking = branch.tracking_branch()
    if tracking is None:
        return {"state": "NO_REMOTE"}

    ahead = int(repo.git.rev_list("--count", f"{tracking}..{branch}"))
    behind = int(repo.git.rev_list("--count", f"{branch}..{tracking}"))

    if ahead > 0:
        return {"state": "AHEAD_OF_REMOTE"}
    if behind > 0:
        return {"state": "BEHIND_REMOTE"}
    return {"state": "UP_TO_DATE"}


def show_diff() -> dict[str, object]:
    repo = _repo()
    branch = _branch_name(repo)
    remote_ref = f"origin/{branch}"

    try:
        repo.refs[remote_ref]
    except IndexError:
        return {"commits": [], "current_ref": repo.head.commit.hexsha[:8], "remote_ref": None}

    current_sha = repo.head.commit.hexsha[:8]
    remote_sha = repo.refs[remote_ref].commit.hexsha[:8]

    commits = []
    for commit in repo.iter_commits(f"HEAD..{remote_ref}"):
        commits.append(
            {
                "sha": commit.hexsha[:8],
                "message": commit.message.strip().split("\n")[0],
            }
        )

    return {
        "commits": commits,
        "current_ref": current_sha,
        "remote_ref": remote_sha,
    }


def apply_update() -> dict[str, object]:
    repo = _repo()

    if repo.is_dirty(untracked_files=True):
        raise RuntimeError("Working tree has uncommitted changes. Stash or commit first.")

    branch = _branch_name(repo)
    remote_ref = f"origin/{branch}"

    try:
        repo.refs[remote_ref]
    except IndexError as e:
        raise RuntimeError(f"No remote ref {remote_ref} found. Run 'update fetch' first.") from e

    local_sha = repo.head.commit.hexsha
    remote_sha = repo.refs[remote_ref].commit.hexsha

    if local_sha == remote_sha:
        return {"ref": local_sha[:8], "system_migrations_applied": [], "already_up_to_date": True}

    logger.info(f"Checking out {remote_ref}...")
    try:
        repo.git.checkout("-fB", branch, remote_ref)
        repo.heads[branch].set_tracking_branch(repo.refs[remote_ref])
    except IndexError:
        repo.git.checkout("-f", remote_ref)
    repo.git.clean("-fd")

    logger.info("Running pixi install...")
    _run_pixi_install()

    logger.info("Running system migrations...")
    applied = _run_migrations_reexec()

    return {
        "ref": repo.head.commit.hexsha[:8],
        "system_migrations_applied": applied,
    }


def _run_pixi_install() -> None:
    result = subprocess.run(
        ["/home/host/.pixi/bin/pixi", "install"],
        cwd=_PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pixi install failed (exit {result.returncode}):\n{result.stderr}")


def _run_migrations_reexec() -> list[int]:
    """Re-exec the agent to run migrations with the newly-installed code."""
    result = subprocess.run(
        [sys.executable, "-m", "openhost_system_agent.migrations.runner"],
        cwd=_PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"System migrations failed:\n{result.stderr}")

    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []


def set_remote_url(url: str) -> dict[str, object]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _KNOWN_SCHEMES:
        url = "https://" + url
        parsed = urllib.parse.urlparse(url)

    ref: str | None = None
    if "@" in parsed.path:
        base_path, ref = parsed.path.rsplit("@", 1)
        url = parsed._replace(path=base_path).geturl()

    repo = _repo()
    try:
        with _get_remote(repo).config_writer as cw:
            cw.set("url", url)
    except RuntimeError:
        repo.create_remote("origin", url)

    if ref:
        _get_remote(repo).fetch()
        try:
            repo.refs[f"origin/{ref}"]
            repo.git.checkout("-fB", ref, f"origin/{ref}")
            repo.heads[ref].set_tracking_branch(repo.refs[f"origin/{ref}"])
        except IndexError:
            repo.git.checkout("-f", ref)
        repo.git.clean("-fd")

    return {"url": _strip_credentials(url), "ref": ref or _branch_name(repo)}


def get_remote_info() -> dict[str, object]:
    repo = _repo()
    remote = _get_remote(repo)
    url = _strip_credentials(remote.url) if remote.url else None
    return {"url": url, "ref": _branch_name(repo)}
