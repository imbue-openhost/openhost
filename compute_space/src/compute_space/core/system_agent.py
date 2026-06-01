from __future__ import annotations

import json
import subprocess

import cattrs

from compute_space.core.util import async_wrap
from openhost_system_agent.protocol import ApplyResult
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo


class SystemAgentError(Exception):
    pass


def _call_system_agent_sync[ResultT](result_type: type[ResultT], *args: str, timeout: int = 300) -> ResultT:
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
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemAgentError(f"Invalid JSON from system agent: {result.stdout}") from e

    try:
        return cattrs.structure(raw, result_type)
    except (cattrs.ClassValidationError, KeyError, TypeError) as e:
        raise SystemAgentError(f"Unexpected response shape from system agent: {e}") from e


@async_wrap
def system_agent_fetch() -> FetchResult:
    return _call_system_agent_sync(FetchResult, "update", "fetch")


@async_wrap
def system_agent_show_diff() -> DiffResult:
    return _call_system_agent_sync(DiffResult, "update", "show_diff")


@async_wrap
def system_agent_apply() -> ApplyResult:
    return _call_system_agent_sync(ApplyResult, "update", "apply", timeout=600)


@async_wrap
def system_agent_set_remote(url: str) -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "set_remote", url, timeout=120)


@async_wrap
def system_agent_get_remote() -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "get_remote")


@async_wrap
def system_agent_status() -> MigrationStatus:
    return _call_system_agent_sync(MigrationStatus, "status")
