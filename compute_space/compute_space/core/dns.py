"""Manage the VM-side CoreDNS zone file.

The VM runs CoreDNS authoritative for its zone domain (e.g. alice.host.imbue.com).
This module writes and updates the zone file. CoreDNS watches for SOA serial
changes and auto-reloads.

For DNS-01 ACME challenges, the router calls set_txt() to add a TXT record,
waits for CoreDNS to pick it up, then calls clear_txt() after the cert is issued.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader

from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR))

_SERIAL_RE = re.compile(r"^(\s+)(\d+)(\s+;\s*serial\s*)$", re.MULTILINE)


def start_coredns(
    zone_domain: str, public_ip: str, corefile_path: Path, zonefile_path: Path
) -> subprocess.Popen[bytes]:
    """Write CoreDNS config + zone file, start CoreDNS, return the process."""

    # Write Corefile. this is coredns's config.
    corefile = _jinja_env.get_template("Corefile").render(
        zone_domain=zone_domain,
        zone_file_path=zonefile_path,
    )
    with open(corefile_path, "w") as f:
        f.write(corefile)

    # Write zone file. this is the actual DNS data. CoreDNS watches for changes to this file and auto-reloads.
    content = _jinja_env.get_template("zonefile").render(
        zone_domain=zone_domain,
        public_ip=public_ip,
        # Use current timestamp as initial SOA serial. This is simple and ensures it's always increasing on each run.
        serial=int(time.time()),
    )
    with open(zonefile_path, "w") as f:
        f.write(content)

    logger.info(f"Starting CoreDNS for {zone_domain}")
    proc = subprocess.Popen(
        ["coredns", "-conf", corefile_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream_coredns_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(f"[coredns] {line.decode(errors='replace').rstrip()}")
        proc.wait()
        logger.warning(f"CoreDNS exited with code {proc.returncode}")

    threading.Thread(target=_stream_coredns_logs, args=(proc,), daemon=True).start()
    logger.info(f"Started CoreDNS for {zone_domain} (pid {proc.pid})")
    return proc


def _bump_serial(content: str) -> str:
    """Read the current SOA serial from the zone file content and increment it.

    Incrementing the SOA serial is necessary to trigger CoreDNS to reload the zone file.
    """
    m = _SERIAL_RE.search(content)
    if not m:
        raise ValueError("Could not find SOA serial in zone file")
    new_serial = int(m.group(2)) + 1
    return _SERIAL_RE.sub(f"{m.group(1)}{new_serial}{m.group(3)}", content, count=1)


def set_txt(
    zone_file_path: Path,
    name: str,
    values: str | list[str],
) -> None:
    """Append TXT record(s) to the zone file and bump the SOA serial.

    For ACME DNS-01 challenges, name is typically '_acme-challenge'.
    values can be a single string or a list of strings (for multiple
    authorizations, e.g. base domain + wildcard).
    """
    if isinstance(values, str):
        values = [values]
    with open(zone_file_path) as f:
        content = f.read()
    content = _bump_serial(content)
    for v in values:
        content += f'{name}   IN TXT  "{v}"\n'
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Set {len(values)} TXT record(s) {name}")


def clear_txt(zone_file_path: Path) -> None:
    """Remove all TXT records from the zone file and bump the SOA serial."""
    with open(zone_file_path) as f:
        content = f.read()
    lines = [line for line in content.splitlines() if "IN TXT" not in line]
    content = _bump_serial("\n".join(lines) + "\n")
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Cleared TXT records from {zone_file_path}")
