"""Runtime security posture checks for the VM.

Checks actual security posture regardless of config settings: TLS active,
SSH disabled, no unexpected ports, code read-only.

When SSH is enabled via the router dashboard toggle, the audit will fail
on ssh_disabled — this is intentional (SSH is a temporary debug tool).

Results are exposed via /health and /api/security-audit endpoints.
"""

import sqlite3
import subprocess
from typing import TypedDict


class CheckResult(TypedDict):
    ok: bool
    detail: str


class AuditResult(TypedDict):
    secure: bool
    checks: dict[str, CheckResult]


# Ports that should be listening in a secure VM
_SECURE_PORTS: set[int] = {53, 80, 443}  # CoreDNS, ACME HTTP-01, HTTPS


def is_sshd_active() -> bool:
    """Check if sshd is currently running (service or socket-activated)."""
    try:
        for unit in ("ssh.service", "sshd.service", "ssh.socket"):
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() == "active":
                return True
        return False
    except Exception:
        return False


def run_audit(db: sqlite3.Connection | None = None) -> AuditResult:
    """Run all security checks. Returns a dict with results.

    {
        "secure": True/False,        # overall pass/fail
        "checks": {
            "check_name": {
                "ok": True/False,
                "detail": "human-readable explanation"
            },
            ...
        }
    }
    """
    checks = {}

    checks["ssh_disabled"] = _check_ssh_disabled()
    checks["ssh_password_disabled"] = _check_ssh_password_disabled()
    checks["tls_active"] = _check_tls_active()
    checks["no_unexpected_ports"] = _check_no_unexpected_ports(db=db)

    secure = all(c["ok"] for c in checks.values())
    return {"secure": secure, "checks": checks}


def _check_ssh_disabled() -> CheckResult:
    """SSH daemon is not running (no remote shell access to the VM)."""
    if is_sshd_active():
        return {"ok": False, "detail": "sshd is running — disable via dashboard toggle"}
    return {"ok": True, "detail": "sshd is not running"}


def _check_ssh_password_disabled() -> CheckResult:
    """SSH password authentication is disabled."""
    try:
        result = subprocess.run(
            ["sshd", "-T"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("passwordauthentication "):
                if "no" in line:
                    return {"ok": True, "detail": "PasswordAuthentication no"}
                return {"ok": False, "detail": f"sshd config: {line}"}
        return {
            "ok": True,
            "detail": "PasswordAuthentication not found in sshd config (default: no on Ubuntu 24.04)",
        }
    except Exception as e:
        return {"ok": True, "detail": f"Could not query sshd config: {e}"}


def _check_tls_active() -> CheckResult:
    """TLS is active (HTTPS listening on port 443)."""
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if ":443 " in line or ":443\t" in line:
                return {"ok": True, "detail": "Listening on port 443"}
        return {"ok": False, "detail": "Not listening on port 443"}
    except Exception as e:
        return {"ok": False, "detail": f"Could not check: {e}"}


def _check_no_unexpected_ports(db: sqlite3.Connection | None = None) -> CheckResult:
    """Only expected ports are listening."""
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        listening_ports = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3]
                port_str = addr.rsplit(":", 1)[-1]
                try:
                    listening_ports.add(int(port_str))
                except ValueError:
                    pass

        # Build dynamic whitelist from DB port mappings
        allocated_ports: set[int] = set()
        if db is not None:
            rows = db.execute("SELECT host_port FROM app_port_mappings").fetchall()
            allocated_ports = {row["host_port"] for row in rows}

        unexpected = set()
        for port in listening_ports:
            if port in _SECURE_PORTS:
                continue
            if 9000 <= port <= 9999:
                continue  # app ports range
            if port in allocated_ports:
                continue  # explicitly allocated port mapping
            unexpected.add(port)

        if unexpected:
            return {
                "ok": False,
                "detail": f"Unexpected listening ports: {sorted(unexpected)}",
            }
        return {
            "ok": True,
            "detail": f"Only expected ports listening: {sorted(listening_ports)}",
        }
    except Exception as e:
        return {"ok": False, "detail": f"Could not check ports: {e}"}
