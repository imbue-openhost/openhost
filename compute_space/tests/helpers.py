"""Shared helpers for compute_space tests.

This is a regular module (not a conftest) so it can be imported reliably
regardless of which conftest.py Python resolves first.
"""

from compute_space import COMPUTE_SPACE_PACKAGE_DIR  # noqa: F401 — re-exported
from compute_space.testing import find_uv  # noqa: F401 — re-exported
from compute_space.testing import router_cmd  # noqa: F401 — re-exported
