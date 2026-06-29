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


def _is_detached(repo: git.Repo) -> bool:
    """Return True when HEAD points at a commit rather than a branch."""
    return repo.head.is_detached


_DEFAULT_BRANCH = "main"


def _remote_default_branch(repo: git.Repo) -> str:
    """Best-effort guess of the branch the host should be tracking.

    Prefers the remote's published default branch (origin/HEAD -> origin/<name>),
    then a local branch named `main`, then any local branch, and finally falls
    back to the literal "main". This is used to recover from a detached HEAD,
    where there is no active branch to derive a tracking ref from.
    """
    # origin/HEAD is a symbolic ref pointing at the remote's default branch.
    try:
        symref = str(repo.git.symbolic_ref("refs/remotes/origin/HEAD")).strip()
        # e.g. "refs/remotes/origin/main" -> "main"
        prefix = "refs/remotes/origin/"
        if symref.startswith(prefix):
            return symref[len(prefix) :]
    except git.GitCommandError:
        pass

    if _DEFAULT_BRANCH in [h.name for h in repo.heads]:
        return _DEFAULT_BRANCH
    if repo.heads:
        return repo.heads[0].name
    return _DEFAULT_BRANCH


def _recover_detached_head(repo: git.Repo) -> str:
    """Move a detached HEAD back onto a real, tracking branch.

    Resolves a target branch (see `_remote_default_branch`) and checks it out
    with the remote ref as its upstream so subsequent fetch/apply logic has a
    tracking branch to reason about. Returns the branch name.
    """
    branch = _remote_default_branch(repo)
    remote_ref = f"origin/{branch}"
    try:
        repo.refs[remote_ref]
    except IndexError as e:
        raise RuntimeError(
            f"HEAD is detached and no remote branch '{remote_ref}' exists to recover onto. "
            f"Run 'update fetch' first, or set an explicit branch with "
            f"'update set_remote <url>@<branch>'."
        ) from e

    logger.info(f"HEAD is detached; recovering onto {branch} (tracking {remote_ref})...")
    repo.git.checkout("-fB", branch, remote_ref)
    repo.heads[branch].set_tracking_branch(repo.refs[remote_ref])
    return branch


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

    # A detached HEAD has no branch (and so no tracking branch) to compare
    # against. Surface it explicitly instead of pretending we're up to date —
    # `update apply` knows how to recover from this state.
    if _is_detached(repo):
        return FetchResult(state="DETACHED_HEAD")

    branch = repo.active_branch
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
    # In detached HEAD there is no branch to derive `origin/<branch>` from, so
    # diff against the branch we would recover onto during `update apply`.
    branch = _remote_default_branch(repo) if _is_detached(repo) else _branch_name(repo)
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

    # Capture the checked-out commit *before* any detached-HEAD recovery moves
    # HEAD, so the up-to-date comparison below reflects whether the working tree
    # actually changed (and therefore whether `pixi install` needs to run).
    local_sha = repo.head.commit.hexsha

    if _is_detached(repo):
        branch = _recover_detached_head(repo)
    else:
        branch = _branch_name(repo)
    remote_ref = f"origin/{branch}"

    try:
        repo.refs[remote_ref]
    except IndexError as e:
        raise RuntimeError(f"No remote ref {remote_ref} found. Run 'update fetch' first.") from e

    remote_sha = repo.refs[remote_ref].commit.hexsha

    if local_sha == remote_sha:
        logger.info("Running system migrations...")
        applied = _run_migrations_reexec()
        return ApplyResult(ref=local_sha[:8], system_migrations_applied=applied, already_up_to_date=not applied)

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

    return ApplyResult(ref=repo.head.commit.hexsha[:8], system_migrations_applied=applied, already_up_to_date=False)


def _run_pixi_install() -> None:
    try:
        result = subprocess.run(
            ["/home/host/.pixi/bin/pixi", "install"],
            cwd=_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("pixi install timed out after 300s") from e
    if result.returncode != 0:
        raise RuntimeError(f"pixi install failed (exit {result.returncode}):\n{result.stderr}")


_MIGRATE_SCRIPT = (
    "import json; "
    "from openhost_system_agent.migrations.runner import apply_system_migrations; "
    "print(json.dumps(apply_system_migrations()))"
)


def _run_migrations_reexec() -> list[int]:
    """Run migrations in a subprocess so the freshly-installed code is imported."""
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
