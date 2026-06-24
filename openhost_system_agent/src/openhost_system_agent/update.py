from __future__ import annotations

import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import git
from loguru import logger

from openhost_system_agent.migrations.migration_log import MIGRATIONS_PATH
from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
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


# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3).
# Other v-prefixed tags (e.g. v1.2.0-rc1) are ignored so version parsing
# can't crash on them.
_RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(repo: git.Repo) -> list[str]:
    return sorted([t.name for t in repo.tags if _RELEASE_TAG.fullmatch(t.name)], key=_version_key)


def _current_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--exact-match", "HEAD")
    except git.GitCommandError:
        return None
    return result if _RELEASE_TAG.fullmatch(result) else None


def _latest_ancestor_tag(repo: git.Repo) -> str | None:
    try:
        result: str = repo.git.describe("--tags", "--abbrev=0", "HEAD")
    except git.GitCommandError:
        return None
    return result if _RELEASE_TAG.fullmatch(result) else None


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


def _host_version() -> int:
    return current_host_version(read_log(MIGRATIONS_PATH))


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
    before = _host_version()

    if current == latest:
        logger.info(f"Already on {latest}, running pending migrations...")
    else:
        # Step onto the first tag ahead so the new tag's apply code runs;
        # apply_after_checkout tail-calls forward through the rest itself.
        next_tag = tags[0] if current is None else next(t for t in tags if _version_key(t) > _version_key(current))
        logger.info(f"Checking out {next_tag}...")
        repo.git.checkout(next_tag)
        repo.git.clean("-fd")

    _run_apply()

    # The walk advanced the migration log and HEAD on disk; read the
    # result back rather than parsing it from the subprocess.
    after = _host_version()
    applied = list(range(before + 1, after + 1))
    ref = _current_tag(repo) or _latest_ancestor_tag(repo) or repo.head.commit.hexsha[:8]
    return ApplyResult(ref=ref, system_migrations_applied=applied, already_up_to_date=not applied)


# ── Run checked-out apply code ───────────────────────────────────────

# STABILITY CONTRACT: this invokes the checked-out tag's apply file by
# path and depends only on its exit code. Keep the path and that contract
# stable. Once a new tag is cut, the contract resets to that tag's code.
_APPLY_ENTRYPOINT = _PACKAGE_DIR / "apply_after_checkout.py"


def _run_apply() -> None:
    """Run the checked-out tag's apply step in a fresh interpreter.

    apply_after_checkout tail-calls forward through any remaining tags via
    os.execv, so this is a single subprocess however many tags the host is
    behind. No aggregate timeout: each step bounds its own work (pixi
    install, git ops), so a cap here would kill a legitimate long catch-up.
    """
    result = subprocess.run(
        [sys.executable, str(_APPLY_ENTRYPOINT)],
        cwd=_PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Apply failed after checkout:\n{result.stderr or result.stdout}")


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
