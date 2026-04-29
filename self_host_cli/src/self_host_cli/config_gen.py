"""Generate router_config.toml for local ``openhost up``.

On server deployments, config is managed by ansible.
"""

import os
import sys
from pathlib import Path

from compute_space.config import DefaultConfig

_DEFAULT_DATA_DIR = os.path.expanduser("~/.openhost/local_compute_space")
_CONFIG_PATH = str(Path(_DEFAULT_DATA_DIR) / "config.toml")


def generate_config(
    domain: str,
    port: int = 8080,
    data_dir: str = _DEFAULT_DATA_DIR,
    email: str = "",
) -> str:

    content = DefaultConfig(
        host="0.0.0.0",
        port=port,
        data_root_dir=data_dir,
        zone_domain=domain,
        acme_email=email or None,
        tls_enabled=False,
        start_caddy=False,
    ).to_toml_str()

    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            f.write(content)
    except OSError as e:
        print(f"Error: could not write config {_CONFIG_PATH}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    return _CONFIG_PATH
