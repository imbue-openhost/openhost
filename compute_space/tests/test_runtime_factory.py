"""Tests for the container runtime factory.

The factory is what picks between runtimes based on router config.  Nothing
else in the codebase should know which runtime is in use.
"""

import pytest

from compute_space.core.runtimes import DockerRuntime
from compute_space.core.runtimes import get_runtime


def test_get_runtime_returns_docker_when_requested() -> None:
    runtime = get_runtime("docker")
    assert isinstance(runtime, DockerRuntime)
    assert runtime.name == "docker"


def test_get_runtime_raises_on_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown container_runtime"):
        get_runtime("nonexistent-runtime")


def test_get_runtime_error_lists_supported_runtimes() -> None:
    """The error message should name every supported runtime so operators
    get an actionable hint when they typo the config value."""
    with pytest.raises(ValueError) as exc_info:
        get_runtime("typo")
    assert "docker" in str(exc_info.value)
