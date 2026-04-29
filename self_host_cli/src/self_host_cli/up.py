"""``openhost up`` -- start the OpenHost router.

--domain: enables TLS via ACME.
--zone-domain: enables host-based app subdomain routing without TLS.
"""

import argparse
import ipaddress
import os
import signal
import socket
import subprocess
import sys

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from self_host_cli.config_gen import generate_config

_PID_DIR = os.path.expanduser("~/.openhost/run")
_ROUTER_PID = os.path.join(_PID_DIR, "router.pid")


def _is_public_ip(ip: str) -> bool:
    """Return True if *ip* is a globally routable address."""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _detect_public_ip() -> str:
    """Detect the machine's public IP address.

    Checks the PUBLIC_IP environment variable first (trusted as-is).
    Falls back to ``hostname -I`` (Linux) then a UDP socket probe
    (cross-platform), filtering out private/link-local/loopback addresses.
    Returns empty string if no public IP is found.
    """
    env_ip = os.environ.get("PUBLIC_IP", "").strip()
    if env_ip:
        return env_ip
    # Try hostname -I (works on Linux/Ubuntu)
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip().split()[0]
            if _is_public_ip(ip):
                return ip
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pass
    # Fallback: UDP socket probe (works on macOS and Linux)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
            if _is_public_ip(ip):
                return ip
    except OSError:
        pass
    return ""


def _write_pid(path: str, pid: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(pid))


def _resolve_zone_domain(args: argparse.Namespace) -> str:
    """Resolve effective zone domain from CLI args.

    --domain implies TLS mode and sets zone domain.
    --zone-domain sets zone domain only (no TLS).
    """

    domain = (args.domain or "").strip()
    zone_domain = (getattr(args, "zone_domain", "") or "").strip()

    if domain and zone_domain and domain != zone_domain:
        print(
            "Error: --domain and --zone-domain must match when both are set.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return domain or zone_domain


def run_up(args: argparse.Namespace) -> None:
    """Run the router directly on the host."""
    print("Starting OpenHost...")
    print()

    zone_domain = _resolve_zone_domain(args)
    email = (getattr(args, "email", "") or "").strip()
    config_path = generate_config(port=args.port, domain=zone_domain, email=email)
    print()

    public_ip = _detect_public_ip()
    if public_ip:
        print(f"  Public IP: {public_ip}")
    if zone_domain:
        print(f"  Domain:    {zone_domain} (host-based app subdomain routing)")
    print(f"  Router:    http://localhost:{args.port}")

    env = os.environ.copy()
    env["OPENHOST_ROUTER_CONFIG"] = config_path

    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "compute_space",
    ]

    if args.foreground:
        print(f"Starting router on http://localhost:{args.port} (Ctrl-C to stop)...")
        proc = subprocess.Popen(cmd, cwd=COMPUTE_SPACE_PACKAGE_DIR, env=env)
        _write_pid(_ROUTER_PID, proc.pid)

        def _handle_signal(signum: int, _frame: object) -> None:
            proc.send_signal(signum)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            rc = proc.wait()
            sys.exit(rc)
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=COMPUTE_SPACE_PACKAGE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_pid(_ROUTER_PID, proc.pid)
        print(f"Router started on http://localhost:{args.port} (pid {proc.pid}).")
        print("Run 'openhost down' to stop.")
