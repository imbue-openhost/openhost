"""Container runtime abstraction.

The router talks to a pluggable ``ContainerRuntime`` for all image build and
container lifecycle operations.  Today only the Docker runtime is shipped;
future runtimes (e.g. rootless Podman) will be wired through the same
interface.

Code outside this package should import the free functions from
``compute_space.core.containers`` (which delegate to the selected runtime),
not call runtime classes directly.
"""

from compute_space.core.runtimes.base import ContainerRuntime
from compute_space.core.runtimes.docker import DockerRuntime
from compute_space.core.runtimes.factory import get_runtime

__all__ = ["ContainerRuntime", "DockerRuntime", "get_runtime"]
