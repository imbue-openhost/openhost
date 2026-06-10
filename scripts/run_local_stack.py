"""Run a full OpenHost stack locally for browser testing.

HTTP-only, bound to loopback, with a ``*.localhost`` zone domain — browsers and the OS
resolver send any ``*.localhost`` name to loopback, so no DNS or /etc/hosts setup is
needed.  Apps run in rootless podman containers exactly as on a real server.

Usage:
    pixi run -e dev python scripts/run_local_stack.py [--fresh] [--port 8080] [--default-apps]

Then open http://home.localhost:8080/ in a browser.  On first run, /setup asks you to
pick an owner password.  Deployed apps are served at http://<app>.home.localhost:8080/.

Data persists in --data-dir across restarts; use --fresh to start over.  App containers
are not children of the router and keep running after it exits (the router re-adopts them
on restart); use ``podman ps`` / ``podman rm -f openhost-<app>`` to stop them manually.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import DefaultConfig

DEFAULT_APPS = [
    "https://github.com/imbue-openhost/secrets",
    "file_browser",
    "https://github.com/imbue-openhost/openhost-catalog",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--zone-name", default="home", help="zone is <zone-name>.localhost:<port>")
    parser.add_argument("--data-dir", default="~/.openhost-local-stack")
    parser.add_argument("--fresh", action="store_true", help="wipe the data dir before starting")
    parser.add_argument(
        "--default-apps",
        action="store_true",
        help="deploy the standard default apps (secrets, file_browser, catalog) at setup",
    )
    args = parser.parse_args()

    # resolve() so symlinked paths like /tmp -> /private/tmp become the real path:
    # podman machine on macOS only shares resolved paths (/Users, /private, /var/folders)
    # with the VM, and bind-mount sources must be visible there.
    data_dir = Path(args.data_dir).expanduser().resolve()
    if args.fresh and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    zone_domain = f"{args.zone_name}.localhost:{args.port}"
    config = DefaultConfig(
        zone_domain=zone_domain,
        host="127.0.0.1",
        port=args.port,
        data_root_dir=str(data_dir),
        apps_dir_override=str(OPENHOST_PROJECT_DIR / "apps"),
        tls_enabled=False,
        start_caddy=False,
        claim_token_required=False,
        default_apps=DEFAULT_APPS if args.default_apps else [],
    )
    config.make_all_dirs()
    config_path = data_dir / "config.toml"
    config.to_toml(str(config_path))

    env = os.environ.copy()
    # Strip stray OPENHOST_* vars so they can't override the generated config.
    for key in list(env):
        if key.startswith("OPENHOST_"):
            del env[key]
    env["OPENHOST_ROUTER_CONFIG"] = str(config_path)

    print(f"data dir:  {data_dir}")
    print(f"zone:      {zone_domain}")
    print()
    print(f"  first run:  http://{zone_domain}/setup   (pick an owner password)")
    print(f"  dashboard:  http://{zone_domain}/dashboard")
    print(f"  apps:       http://<app-name>.{zone_domain}/")
    print()

    proc = subprocess.run(
        [sys.executable, "-m", "compute_space"],
        cwd=str(COMPUTE_SPACE_PACKAGE_DIR),
        env=env,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
