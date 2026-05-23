from __future__ import annotations

import json
import subprocess
from typing import Any

import attr

from compute_space.core.util import async_wrap


class SystemAgentError(Exception):
    pass


@attr.s(auto_attribs=True, frozen=True)
class FetchResult:
    state: str


@attr.s(auto_attribs=True, frozen=True)
class DiffCommit:
    sha: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class DiffResult:
    commits: list[DiffCommit]
    current_ref: str
    remote_ref: str | None


@attr.s(auto_attribs=True, frozen=True)
class ApplyResult:
    ref: str
    system_migrations_applied: list[int]
    already_up_to_date: bool = False


@attr.s(auto_attribs=True, frozen=True)
class RemoteInfo:
    url: str | None
    ref: str


@attr.s(auto_attribs=True, frozen=True)
class MigrationStatus:
    ok: bool
    reason: str
    message: str
    current_version: int
    expected_version: int


def _call_system_agent_sync(*args: str, timeout: int = 300) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["sudo", "openhost_system_agent", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise SystemAgentError("openhost_system_agent not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise SystemAgentError(f"openhost_system_agent timed out after {timeout}s") from e

    if result.returncode != 0:
        try:
            body = json.loads(result.stdout)
            error = body.get("error", result.stderr)
        except (json.JSONDecodeError, ValueError):
            error = result.stderr or result.stdout
        raise SystemAgentError(str(error))

    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
        return parsed
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemAgentError(f"Invalid JSON from system agent: {result.stdout}") from e


@async_wrap
def agent_fetch() -> FetchResult:
    data = _call_system_agent_sync("update", "fetch")
    return FetchResult(state=str(data.get("state", "UNKNOWN")))


@async_wrap
def agent_show_diff() -> DiffResult:
    data = _call_system_agent_sync("update", "show_diff")
    commits = [DiffCommit(sha=c["sha"], message=c["message"]) for c in data.get("commits", [])]
    return DiffResult(
        commits=commits,
        current_ref=str(data.get("current_ref", "")),
        remote_ref=data.get("remote_ref"),
    )


@async_wrap
def agent_apply() -> ApplyResult:
    data = _call_system_agent_sync("update", "apply", timeout=600)
    return ApplyResult(
        ref=str(data.get("ref", "")),
        system_migrations_applied=data.get("system_migrations_applied", []),
        already_up_to_date=bool(data.get("already_up_to_date", False)),
    )


@async_wrap
def agent_set_remote(url: str) -> RemoteInfo:
    data = _call_system_agent_sync("update", "set_remote", url, timeout=120)
    return RemoteInfo(url=data.get("url"), ref=str(data.get("ref", "")))


@async_wrap
def agent_get_remote() -> RemoteInfo:
    data = _call_system_agent_sync("update", "get_remote")
    return RemoteInfo(url=data.get("url"), ref=str(data.get("ref", "")))


@async_wrap
def agent_status() -> MigrationStatus:
    data = _call_system_agent_sync("status")
    return MigrationStatus(
        ok=bool(data.get("ok")),
        reason=str(data.get("reason", "")),
        message=str(data.get("message", "")),
        current_version=int(data.get("current_version", 0)),
        expected_version=int(data.get("expected_version", 0)),
    )
