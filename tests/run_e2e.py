#!/usr/bin/env python3
"""Run e2e tests against an OpenHost instance configured via the ``oh`` CLI.

Usage::

    python tests/run_e2e.py                                    # all e2e tests, default oh instance
    python tests/run_e2e.py --instance dev2                    # specific oh instance
    python tests/run_e2e.py -k oauth                           # subset via pytest -k
    python tests/run_e2e.py tests/test_e2e_oauth.py            # specific test file
    python tests/run_e2e.py tests/test_e2e_oauth.py -k browser # combine file + pattern

Reads the instance URL and API token from ``~/.openhost/compute_space_cli.toml``
(the same config used by the ``oh`` CLI), sets ``OPENHOST_DOMAIN`` and
``OPENHOST_TOKEN``, then runs pytest with any extra arguments passed through.
"""

import os
import subprocess
import sys
from pathlib import Path

from compute_space_cli.config import MultiConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEST_PATHS = ["tests/test_e2e.py"]


def main() -> int:

    # Parse --instance ourselves; everything else goes to pytest.
    instance_name = None
    pytest_args: list[str] = []
    it = iter(sys.argv[1:])
    for arg in it:
        if arg == "--instance":
            instance_name = next(it, None)
            if not instance_name:
                print("--instance requires a value", file=sys.stderr)
                return 1
        elif arg.startswith("--instance="):
            instance_name = arg.split("=", 1)[1]
        else:
            pytest_args.append(arg)

    cfg = MultiConfig.load()
    inst = cfg.resolve(instance_name)

    domain = inst.hostname

    # If no test paths were provided, use defaults.
    has_test_path = any(a.endswith(".py") or os.path.isdir(a) for a in pytest_args)
    test_paths = [] if has_test_path else DEFAULT_TEST_PATHS

    env = os.environ.copy()
    env["OPENHOST_DOMAIN"] = domain
    env["OPENHOST_TOKEN"] = inst.token

    cmd = ["uv", "run", "--group", "dev", "pytest", *test_paths, "-v", "-s", "--timeout=600", *pytest_args]

    print(f"Instance: {inst.url}")
    print(f"Domain:   {domain}")
    print(f"Running:  {' '.join(cmd)}")
    print()

    return subprocess.call(cmd, env=env, cwd=str(REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
