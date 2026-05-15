from __future__ import annotations

import json
import subprocess

from compute_space.core.util import async_wrap


class SystemAgentError(Exception):
    pass


@async_wrap
def _call_system_agent(*args: str, timeout: int = 300) -> dict[str, object]:
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
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemAgentError(f"Invalid JSON from system agent: {result.stdout}") from e


async def agent_fetch() -> dict[str, object]:
    return await _call_system_agent("update", "fetch")


async def agent_show_diff() -> dict[str, object]:
    return await _call_system_agent("update", "show_diff")


async def agent_apply() -> dict[str, object]:
    return await _call_system_agent("update", "apply", timeout=600)


async def agent_set_remote(url: str) -> dict[str, object]:
    return await _call_system_agent("update", "set_remote", url, timeout=120)


async def agent_get_remote() -> dict[str, object]:
    return await _call_system_agent("update", "get_remote")
