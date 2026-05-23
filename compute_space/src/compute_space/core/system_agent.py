from __future__ import annotations

import json
import subprocess
from typing import Any

import attr
import cattrs

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
    already_up_to_date: bool


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
def system_agent_fetch() -> FetchResult:
    return cattrs.structure(_call_system_agent_sync("update", "fetch"), FetchResult)


@async_wrap
def system_agent_show_diff() -> DiffResult:
    return cattrs.structure(_call_system_agent_sync("update", "show_diff"), DiffResult)


@async_wrap
def system_agent_apply() -> ApplyResult:
    return cattrs.structure(_call_system_agent_sync("update", "apply", timeout=600), ApplyResult)


@async_wrap
def system_agent_set_remote(url: str) -> RemoteInfo:
    return cattrs.structure(_call_system_agent_sync("update", "set_remote", url, timeout=120), RemoteInfo)


@async_wrap
def system_agent_get_remote() -> RemoteInfo:
    return cattrs.structure(_call_system_agent_sync("update", "get_remote"), RemoteInfo)


@async_wrap
def system_agent_status() -> MigrationStatus:
    return cattrs.structure(_call_system_agent_sync("status"), MigrationStatus)
