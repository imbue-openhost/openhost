from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import git
from loguru import logger

from openhost_system_agent.protocol import ApplyResult
from openhost_system_agent.protocol import DiffCommit
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import RemoteInfo

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent.parent.parent


def _repo() -> git.Repo:
    return git.Repo(_PROJECT_DIR)


def _get_remote(repo: git.Repo) -> git.Remote:
    try:
        return repo.remote("origin")
    except (AttributeError, ValueError) as e:
        raise RuntimeError("remote 'origin' is not set") from e


_KNOWN_SCHEMES = {"http", "https", "ssh", "git", "file"}


def _strip_credentials(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port = f"{host_port}:{parsed.port}"
        return parsed._replace(netloc=host_port).geturl()
    return url


# ── Tag helpers ──────────────────────────────────────────────────────


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(repo: git.Repo) -> list[str]:
    return sorted([t.name for t in repo.tags if t.name.startswith("v")], key=_version_key)


def _current_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--exact-match", "HEAD")
        return result
    except git.GitCommandError:
        return None


def _latest_ancestor_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--abbrev=0", "HEAD")
        return result
    except git.GitCommandError:
        return None


# ── Fetch / diff / apply ─────────────────────────────────────────────


def fetch_updates() -> FetchResult:
    repo = _repo()
    _get_remote(repo)
    repo.git.fetch("origin", "--tags")

    if repo.is_dirty(untracked_files=True):
        return FetchResult(state="DIRTY")

    tags = _get_sorted_tags(repo)
    if not tags:
        return FetchResult(state="UP_TO_DATE")

    latest = tags[-1]
    current = _current_tag(repo) or _latest_ancestor_tag(repo)

    if current is None or _version_key(current) < _version_key(latest):
        return FetchResult(state="BEHIND_REMOTE")
    return FetchResult(state="UP_TO_DATE")


def show_diff() -> DiffResult:
    repo = _repo()
    tags = _get_sorted_tags(repo)
    current = _current_tag(repo) or _latest_ancestor_tag(repo) or repo.head.commit.hexsha[:8]

    if not tags:
        return DiffResult(commits=[], current_ref=current, remote_ref=None)

    latest = tags[-1]
    commits = []
    for commit in repo.iter_commits(f"{current}..{latest}"):
        commits.append(
            DiffCommit(
                sha=commit.hexsha[:8],
                message=str(commit.message).strip().split("\n")[0],
            )
        )
    return DiffResult(commits=commits, current_ref=current, remote_ref=latest)


def apply_update() -> ApplyResult:
    repo = _repo()

    if repo.is_dirty(untracked_files=True):
        raise RuntimeError("Working tree has uncommitted changes. Stash or commit first.")

    repo.git.fetch("origin", "--tags")
    tags = _get_sorted_tags(repo)
    if not tags:
        raise RuntimeError("No tags found on remote. Tag a release first.")

    latest = tags[-1]
    current = _current_tag(repo) or _latest_ancestor_tag(repo)

    if current == latest:
        logger.info(f"Already on {latest}, running pending migrations...")
        return _reexec_apply()

    # Find the next tag after our current position and start the walk.
    if current is None:
        next_tag = tags[0]
    else:
        later = [t for t in tags if _version_key(t) > _version_key(current)]
        if not later:
            logger.info(f"Already on {current}, running pending migrations...")
            return _reexec_apply()
        next_tag = later[0]

    logger.info(f"Checking out {next_tag}...")
    repo.git.checkout(next_tag)
    repo.git.clean("-fd")

    return _reexec_apply()


# ── Re-exec into checked-out code ────────────────────────────────────

# STABILITY CONTRACT: the prior tag's _reexec_apply calls this file by
# path. Keep the path stable relative to the prior tag's caller. Once a
# new tag is cut, the contract resets.
_APPLY_ENTRYPOINT = _PACKAGE_DIR / "apply_after_checkout.py"


def _reexec_apply() -> ApplyResult:
    try:
        result = subprocess.run(
            [sys.executable, str(_APPLY_ENTRYPOINT)],
            cwd=_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Re-exec apply timed out after 600s") from e

    if result.returncode != 0:
        try:
            body = json.loads(result.stdout)
            error = body.get("error", result.stderr)
        except (json.JSONDecodeError, ValueError):
            error = result.stderr or result.stdout
        raise RuntimeError(f"Apply failed after checkout:\n{error}")

    try:
        raw = json.loads(result.stdout)
        return ApplyResult(
            ref=raw["ref"],
            system_migrations_applied=raw["system_migrations_applied"],
            already_up_to_date=raw["already_up_to_date"],
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid apply result: {result.stdout}") from e


# ── Remote management ────────────────────────────────────────────────


def set_remote_url(url: str) -> RemoteInfo:
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

    return RemoteInfo(
        url=_strip_credentials(url), ref=ref or (tags[-1] if (tags := _get_sorted_tags(repo)) else "HEAD")
    )


def get_remote_info() -> RemoteInfo:
    repo = _repo()
    remote = _get_remote(repo)
    url = _strip_credentials(remote.url) if remote.url else None
    current = _current_tag(repo) or _latest_ancestor_tag(repo) or repo.head.commit.hexsha[:8]
    return RemoteInfo(url=url, ref=current)
