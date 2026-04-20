"""Factory that picks the container runtime based on router config.

This indirection exists so the rest of the codebase never has to know which
runtime is in use — it just imports the free functions from
``compute_space.core.containers`` (which delegate through this factory).
"""

from __future__ import annotations

from compute_space.config import get_config
from compute_space.core.runtimes.base import ContainerRuntime
from compute_space.core.runtimes.docker import DockerRuntime

_SUPPORTED_RUNTIMES: dict[str, type[ContainerRuntime]] = {
    "docker": DockerRuntime,
}

_DEFAULT_RUNTIME_NAME = "docker"


def _resolve_runtime_name() -> str:
    """Return the configured runtime name, or the default when no Quart
    application context is available (e.g. CLI or unit tests).
    """
    try:
        return get_config().container_runtime
    except RuntimeError:
        # Outside a Quart application context — fall back to the historical
        # default so callers don't have to thread a Config object through
        # every call site.
        return _DEFAULT_RUNTIME_NAME


def get_runtime(runtime_name: str | None = None) -> ContainerRuntime:
    """Return a ``ContainerRuntime`` instance for the configured runtime.

    If ``runtime_name`` is ``None``, the current Quart app config is consulted
    (so the runtime selection lives in the same place as every other router
    config value).  When called outside a request/app context, the historical
    default runtime is used.

    Raises ``ValueError`` on an unknown runtime name.
    """
    if runtime_name is None:
        runtime_name = _resolve_runtime_name()

    try:
        cls = _SUPPORTED_RUNTIMES[runtime_name]
    except KeyError:
        supported = ", ".join(sorted(_SUPPORTED_RUNTIMES))
        raise ValueError(f"Unknown container_runtime {runtime_name!r}; supported: {supported}") from None
    return cls()
