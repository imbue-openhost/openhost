"""Manage the VM-side CoreDNS zone file.

The VM runs CoreDNS authoritative for its zone domain (e.g. alice.host.imbue.com).
This module writes and updates the zone file. CoreDNS watches for SOA serial
changes and auto-reloads.

For DNS-01 ACME challenges, the router calls append_txt_records() to add TXT
records, waits for CoreDNS to pick them up, then calls clear_txt() after the cert
is issued.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import attr
from jinja2 import Environment
from jinja2 import FileSystemLoader

from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR))

_SERIAL_RE = re.compile(r"^(\s+)(\d+)(\s+;\s*serial\s*)$", re.MULTILINE)


def _coredns_bind_ip(public_ip: str) -> str:
    """Return the local address CoreDNS should bind for authoritative DNS.

    Binding wildcard :53 conflicts with Podman's aardvark-dns on 10.89.0.1:53.
    Binding the configured public IP works on hosts where that IP is assigned to
    an interface (for example Hetzner), but fails on AWS/GCP where public IPs are
    NATed to a private VM address. The default-route source address is the local
    interface address that receives that NATed traffic.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return public_ip


def start_coredns(
    zone_domain: str, public_ip: str, corefile_path: Path, zonefile_path: Path
) -> subprocess.Popen[bytes]:
    """Write CoreDNS config + zone file, start CoreDNS, return the process."""

    # Write Corefile. this is coredns's config.
    corefile = _jinja_env.get_template("Corefile").render(
        zone_domain=zone_domain,
        bind_ip=_coredns_bind_ip(public_ip),
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


@attr.s(auto_attribs=True, frozen=True)
class TxtRecord:
    """A TXT record to publish.

    ``record_name`` is written into the zone file verbatim, so the caller chooses
    the addressing: a name relative to the zone's $ORIGIN (e.g. '_acme-challenge')
    has CoreDNS append the origin, while an absolute FQDN ending in '.' is honored
    as-is.
    """

    record_name: str
    record_value: str


def append_txt_records(zone_file_path: Path, records: list[TxtRecord]) -> None:
    """Append TXT record(s) to the zone file and bump the SOA serial.

    Each record's ``record_name`` is written verbatim (see TxtRecord), so this
    serves both local DNS-01 challenges (relative '_acme-challenge' names) and
    openhost-cert-api broker challenges (absolute FQDNs). Bumping the SOA serial
    triggers a CoreDNS reload.
    """
    with open(zone_file_path) as f:
        content = f.read()
    content = _bump_serial(content)
    for record in records:
        content += f'{record.record_name}   IN TXT  "{record.record_value}"\n'
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Appended {len(records)} TXT record(s)")


def clear_txt(zone_file_path: Path) -> None:
    """Remove all TXT records from the zone file and bump the SOA serial."""
    with open(zone_file_path) as f:
        content = f.read()
    lines = [line for line in content.splitlines() if "IN TXT" not in line]
    content = _bump_serial("\n".join(lines) + "\n")
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Cleared TXT records from {zone_file_path}")
