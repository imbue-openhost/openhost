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


def fetch_updates() -> FetchResult:
    repo = _repo()
    remote = _get_remote(repo)
    remote.fetch()

    if repo.is_dirty(untracked_files=True):
        return FetchResult(state="DIRTY")

    try:
        branch = repo.active_branch
    except TypeError:
        return FetchResult(state="UP_TO_DATE")

    tracking = branch.tracking_branch()
    if tracking is None:
        raise RuntimeError(f"Branch {branch.name} has no tracking branch set")

    ahead = int(repo.git.rev_list("--count", f"{tracking}..{branch}"))
    behind = int(repo.git.rev_list("--count", f"{branch}..{tracking}"))

    if ahead > 0:
        return FetchResult(state="AHEAD_OF_REMOTE")
    if behind > 0:
        return FetchResult(state="BEHIND_REMOTE")
    return FetchResult(state="UP_TO_DATE")


def show_diff() -> DiffResult:
    repo = _repo()
    branch = _branch_name(repo)
    remote_ref = f"origin/{branch}"

    try:
        repo.refs[remote_ref]
    except IndexError:
        return DiffResult(commits=[], current_ref=repo.head.commit.hexsha[:8], remote_ref=None)

    current_sha = repo.head.commit.hexsha[:8]
    remote_sha = repo.refs[remote_ref].commit.hexsha[:8]

    commits = []
    for commit in repo.iter_commits(f"HEAD..{remote_ref}"):
        commits.append(
            DiffCommit(
                sha=commit.hexsha[:8],
                message=str(commit.message).strip().split("\n")[0],
            )
        )

    return DiffResult(commits=commits, current_ref=current_sha, remote_ref=remote_sha)


def apply_update() -> ApplyResult:
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
        return _apply_current(repo)

    logger.info(f"Checking out {remote_ref}...")
    try:
        repo.git.checkout("-fB", branch, remote_ref)
        repo.heads[branch].set_tracking_branch(repo.refs[remote_ref])
    except IndexError:
        repo.git.checkout("-f", remote_ref)
    repo.git.clean("-fd")

    # Re-exec into the freshly checked-out code so the new apply logic
    # controls the pre_install → pixi install → post_install sequence.
    # This is the critical handoff: from this point forward, the NEW code
    # decides what runs and in what order.
    return _reexec_apply()


def _apply_current(repo: git.Repo) -> ApplyResult:
    """Code and checkout are at the same ref — just run pending migrations."""
    applied = _run_migrations_reexec()
    ref = repo.head.commit.hexsha[:8]
    return ApplyResult(ref=ref, system_migrations_applied=applied, already_up_to_date=not applied)


# ── Re-exec into new code ────────────────────────────────────────────

# Path to the apply entrypoint, relative to the repo root. After checkout
# this file belongs to the NEW code, so the new code controls the full
# pre_install → pixi install → post_install sequence.
#
# STABILITY CONTRACT: this path is load-bearing for every deployed host.
# Old hosts invoke it from their frozen _reexec_apply. Do not move or
# rename it without a migration that updates the caller.
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


# ── Legacy migration re-exec (for the up-to-date path) ──────────────

_MIGRATE_SCRIPT = (
    "import json; "
    "from openhost_system_agent.migrations.runner import apply_system_migrations; "
    "print(json.dumps(apply_system_migrations()))"
)


def _run_migrations_reexec() -> list[int]:
    try:
        result = subprocess.run(
            [sys.executable, "-c", _MIGRATE_SCRIPT],
            cwd=_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("System migrations timed out after 300s") from e
    if result.returncode != 0:
        raise RuntimeError(f"System migrations failed:\n{result.stderr}")

    try:
        parsed: list[int] = json.loads(result.stdout)
        return parsed
    except (json.JSONDecodeError, ValueError):
        return []


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

    return RemoteInfo(url=_strip_credentials(url), ref=ref or _branch_name(repo))


def get_remote_info() -> RemoteInfo:
    repo = _repo()
    remote = _get_remote(repo)
    url = _strip_credentials(remote.url) if remote.url else None
    return RemoteInfo(url=url, ref=_branch_name(repo))
