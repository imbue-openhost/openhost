import urllib.parse
from pathlib import Path

import git

from compute_space.core.util import async_wrap


class RemoteNotSetError(Exception):
    pass


class NoRemoteBranchForLocalBranch(Exception):
    pass


_KNOWN_SCHEMES = {"http", "https", "ssh", "git", "file"}


def parse_repo_url(repo_url: str) -> tuple[str, str | None]:
    """Parse a repo URL with optional @ref suffix (pip-style).

    Returns (base_url, ref) where ref is a branch, tag, or commit hash, or None.
    """
    # Allow bare hostnames like "github.com/user/repo" without a scheme.
    # urlparse misidentifies credentials (e.g. "oauth2:TOKEN@host") as a scheme,
    # so we only trust schemes we actually recognise.
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.scheme not in _KNOWN_SCHEMES:
        repo_url = "https://" + repo_url
        parsed = urllib.parse.urlparse(repo_url)
    path = parsed.path
    if "@" in path:
        base_path, ref = path.rsplit("@", 1)
        base_url = parsed._replace(path=base_path).geturl()
        return base_url, ref
    return repo_url, None


def _get_remote(repo: git.Repo) -> git.Remote:
    try:
        return repo.remote("origin")
    except (AttributeError, ValueError) as e:
        raise RemoteNotSetError("remote 'origin' is not set") from e


@async_wrap
def validate_repo(repo_path: Path) -> None:
    """Check if the given path is a valid git repository.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
    """
    git.Repo(repo_path)


@async_wrap
def get_current_ref(repo_path: Path) -> str:
    """Return the current branch name, or the short commit hash if in detached HEAD state."""
    repo = git.Repo(repo_path)
    try:
        return repo.active_branch.name
    except TypeError:
        return repo.head.commit.hexsha[:8]


def _strip_credentials(url: str) -> str:
    """Remove userinfo (OAuth tokens, passwords) from a URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port = f"{host_port}:{parsed.port}"
        return parsed._replace(netloc=host_port).geturl()
    return url


@async_wrap
def get_remote_url(repo_path: Path) -> str | None:
    """Returns the remote URL with any credentials stripped.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote
    """
    repo = git.Repo(repo_path)
    url = _get_remote(repo).url
    return _strip_credentials(url) if url else None


@async_wrap
def is_dirty(repo_path: Path) -> bool:
    """
    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
    """
    return git.Repo(repo_path).is_dirty(untracked_files=True)


@async_wrap
def fetch(repo_path: Path) -> None:
    """
    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote
    """
    repo = git.Repo(repo_path)
    _get_remote(repo).fetch()


@async_wrap
def count_commits_vs_remote(repo_path: Path) -> tuple[int, int]:
    """Returns (ahead, behind) commit counts compared to the tracking branch.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote or no tracking branch is set
    """
    repo = git.Repo(repo_path)
    try:
        branch = repo.active_branch
    except TypeError:
        # detached head; no new commits
        return 0, 0
    tracking = branch.tracking_branch()

    if tracking is None:
        raise NoRemoteBranchForLocalBranch(f"{branch.name} has no tracking branch set")

    behind = int(repo.git.rev_list("--count", f"{branch}..{tracking}"))
    ahead = int(repo.git.rev_list("--count", f"{tracking}..{branch}"))
    return ahead, behind


@async_wrap
def init_repo_if_nonexistent(repo_path: Path) -> None:
    """Initialise a git repo if one doesn't already exist."""
    try:
        git.Repo(repo_path)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        git.Repo.init(repo_path, initial_branch="main")


@async_wrap
def set_remote_url(repo_path: Path, url: str) -> None:
    """Set or create the 'origin' remote to the given URL."""
    repo = git.Repo(repo_path)
    try:
        with _get_remote(repo).config_writer as cw:
            cw.set("url", url)
    except RemoteNotSetError:
        repo.create_remote("origin", url)


@async_wrap
def hard_checkout_ref(repo_path: Path, ref: str) -> None:
    """set local state to match origin/ref, checking out if a branch or detached head if a commit hash."""
    repo = git.Repo(repo_path)
    remote_ref = f"origin/{ref}"
    try:
        repo.refs[remote_ref]
        # It's a branch on the remote — create/reset local branch tracking it
        repo.git.checkout("-fB", ref, remote_ref)
        repo.heads[ref].set_tracking_branch(repo.refs[remote_ref])
    except IndexError:
        # Not a remote branch — treat as a commit hash, detached HEAD
        repo.git.checkout("-f", ref)
    # checkout -f resets tracked files but leaves untracked files behind, which can
    # shadow modules removed/renamed between revisions. Match origin/ref fully.
    repo.git.clean("-fd")
