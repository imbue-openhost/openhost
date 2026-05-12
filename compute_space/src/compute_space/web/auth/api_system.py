import hashlib
import secrets
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.logging import get_log_path
from compute_space.core.auth.security import is_sshd_active
from compute_space.core.auth.security import list_listening_ports
from compute_space.core.auth.security import run_audit
from compute_space.core.storage import is_guard_paused
from compute_space.core.storage import set_guard_paused
from compute_space.core.storage import storage_status
from compute_space.core.updates import is_shutdown_pending
from compute_space.db import get_db
from compute_space.web.auth.middleware import login_required

api_system_bp: Blueprint = Blueprint("api_system", __name__)

DEFAULT_TOKEN_EXPIRY_HOURS: int = 8


# ─── API Tokens ───


@api_system_bp.route("/api/tokens", methods=["GET"])
@login_required
async def api_tokens_list() -> Response:
    db = get_db()
    rows = db.execute("SELECT id, name, expires_at, created_at FROM api_tokens ORDER BY created_at DESC").fetchall()
    now = datetime.now(UTC)
    tokens = []
    for r in rows:
        has_expiry = bool(r["expires_at"])
        expired = has_expiry and datetime.fromisoformat(r["expires_at"]) < now
        tokens.append(
            {
                "id": r["id"],
                "name": r["name"],
                "expires_at": r["expires_at"] or None,
                "created_at": r["created_at"],
                "expired": expired,
            }
        )
    return jsonify(tokens)


@api_system_bp.route("/api/tokens", methods=["POST"])
@login_required
async def api_tokens_create() -> Response | tuple[Response, int]:
    form = await request.form
    name = (form.get("name") or "").strip() or "Untitled"
    expiry_hours_raw = form.get("expiry_hours", "").strip()
    if expiry_hours_raw == "" or expiry_hours_raw.lower() == "never":
        expires_at = None
    else:
        try:
            expiry_hours = float(expiry_hours_raw)
        except (ValueError, TypeError):
            expiry_hours = DEFAULT_TOKEN_EXPIRY_HOURS
        if expiry_hours <= 0:
            return jsonify({"error": "Expiry must be positive"}), 400
        expires_at = datetime.now(UTC) + timedelta(hours=expiry_hours)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    db = get_db()
    db.execute(
        "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
        (name, token_hash, expires_at.isoformat() if expires_at else ""),
    )
    db.commit()

    return jsonify(
        {
            "token": raw_token,
            "name": name,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
    )


@api_system_bp.route("/api/tokens/<int:token_id>", methods=["DELETE"])
@login_required
async def api_tokens_delete(token_id: int) -> Response:
    db = get_db()
    db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
    db.commit()
    return jsonify({"ok": True})


# ─── Compute Space Logs ───


@api_system_bp.route("/api/compute_space_logs")
@login_required
def compute_space_logs() -> Response:
    """Return the compute space log file contents."""
    log_path = get_log_path()
    if log_path is None:
        return Response("Log file not configured", status=503, content_type="text/plain")
    with open(log_path) as f:
        return Response(f.read(), content_type="text/plain")


# ─── Health & Security ───


@api_system_bp.route("/health")
def health() -> ResponseReturnValue:
    if is_shutdown_pending():
        return jsonify({"status": "restarting"}), 503
    audit = run_audit(db=get_db())
    return jsonify({"status": "ok", "security": audit})


@api_system_bp.route("/api/security-audit")
@login_required
def security_audit() -> Response:
    return jsonify(run_audit(db=get_db()))


@api_system_bp.route("/api/listening-ports")
@login_required
def listening_ports() -> Response:
    """Return every TCP port the VM is listening on, with classification."""
    return jsonify({"ports": list_listening_ports(db=get_db())})


@api_system_bp.route("/api/storage-status")
@login_required
def api_storage_status() -> Response:
    return jsonify(storage_status(get_config()))


@api_system_bp.route("/api/storage-guard", methods=["POST"])
@login_required
async def toggle_storage_guard() -> Response:
    """Pause or resume the storage guard."""
    form = await request.form
    paused = (form.get("paused") or "").strip().lower() in ("1", "true", "yes")
    set_guard_paused(paused)
    return jsonify({"guard_paused": is_guard_paused()})


# ─── SSH Toggle ───


@api_system_bp.route("/api/ssh-status")
@login_required
def ssh_status() -> Response:
    return jsonify({"ssh_enabled": is_sshd_active()})


@api_system_bp.route("/toggle-ssh", methods=["POST"])
@login_required
async def toggle_ssh() -> Response:
    if is_sshd_active():
        subprocess.run(
            [
                "sudo",
                "-n",
                "systemctl",
                "stop",
                "ssh.service",
                "sshd.service",
                "ssh.socket",
            ],
            timeout=10,
        )
        subprocess.run(
            [
                "sudo",
                "-n",
                "systemctl",
                "mask",
                "ssh.service",
                "sshd.service",
                "ssh.socket",
            ],
            timeout=10,
        )
        return jsonify({"ssh_enabled": False})
    else:
        subprocess.run(
            [
                "sudo",
                "-n",
                "systemctl",
                "unmask",
                "ssh.service",
                "sshd.service",
                "ssh.socket",
            ],
            timeout=10,
        )
        subprocess.run(["sudo", "-n", "systemctl", "start", "ssh.service"], timeout=10)
        return jsonify({"ssh_enabled": is_sshd_active()})


# ─── Router restart ───


@api_system_bp.route("/api/drop-docker-cache", methods=["POST"])
@login_required
def drop_docker_cache() -> Response | tuple[Response, int]:
    """Drop the container build cache to free disk space."""
    try:
        output = drop_docker_build_cache()
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "output": output})


@api_system_bp.route("/restart_router", methods=["POST"])
@login_required
def restart_router() -> Response:
    """Restart the router systemd service to pick up code changes."""
    subprocess.Popen(
        [
            "sudo",
            "-n",
            "bash",
            "-c",
            "systemctl kill --signal=SIGKILL openhost; systemctl start openhost",
        ],
        start_new_session=True,
    )
    return jsonify({"ok": True})
