import hashlib
import secrets
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Annotated
from typing import Any

import attr
from litestar import Controller
from litestar import Response
from litestar import delete
from litestar import get
from litestar import post
from litestar.enums import RequestEncodingType
from litestar.params import Body

from compute_space.config import get_config
from compute_space.core.auth.security import is_sshd_active
from compute_space.core.auth.security import list_listening_ports
from compute_space.core.auth.security import run_audit
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.logging import get_log_path
from compute_space.core.storage import is_guard_paused
from compute_space.core.storage import set_guard_paused
from compute_space.core.storage import storage_status
from compute_space.core.updates import is_shutdown_pending
from compute_space.db import get_db

DEFAULT_TOKEN_EXPIRY_HOURS: int = 8


@attr.s(auto_attribs=True, frozen=True)
class TokenCreateForm:
    name: str = ""
    expiry_hours: str = ""


@attr.s(auto_attribs=True, frozen=True)
class StorageGuardForm:
    paused: str = ""


class ApiTokensController(Controller):
    path = "/api/tokens"

    @get("/")
    async def list_tokens(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        db = get_db()
        rows = db.execute(
            "SELECT id, name, expires_at, created_at FROM api_tokens ORDER BY created_at DESC"
        ).fetchall()
        now = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for r in rows:
            has_expiry = bool(r["expires_at"])
            expired = has_expiry and datetime.fromisoformat(r["expires_at"]) < now
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "expires_at": r["expires_at"] or None,
                    "created_at": r["created_at"],
                    "expired": expired,
                }
            )
        return out

    @post("/", status_code=200)
    async def create_token(
        self,
        data: Annotated[TokenCreateForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
        user: dict[str, Any],
    ) -> Response[dict[str, Any]]:
        name = (data.name or "").strip() or "Untitled"
        expiry_hours_raw = (data.expiry_hours or "").strip()
        if expiry_hours_raw == "" or expiry_hours_raw.lower() == "never":
            expires_at = None
        else:
            try:
                expiry_hours = float(expiry_hours_raw)
            except (ValueError, TypeError):
                expiry_hours = DEFAULT_TOKEN_EXPIRY_HOURS
            if expiry_hours <= 0:
                return Response(content={"error": "Expiry must be positive"}, status_code=400)
            expires_at = datetime.now(UTC) + timedelta(hours=expiry_hours)

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        db = get_db()
        db.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
            (name, token_hash, expires_at.isoformat() if expires_at else ""),
        )
        db.commit()

        return Response(
            content={
                "token": raw_token,
                "name": name,
                "expires_at": expires_at.isoformat() if expires_at else None,
            }
        )

    @delete("/{token_id:int}", status_code=200)
    async def delete_token(self, token_id: int, user: dict[str, Any]) -> dict[str, bool]:
        db = get_db()
        db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        db.commit()
        return {"ok": True}


@get("/api/compute_space_logs", sync_to_thread=False)
def compute_space_logs(user: dict[str, Any]) -> Response[bytes]:
    log_path = get_log_path()
    if log_path is None:
        return Response(content=b"Log file not configured", status_code=503, media_type="text/plain")
    with open(log_path) as f:
        return Response(content=f.read().encode(), media_type="text/plain")


@get("/health", sync_to_thread=False)
def health() -> Response[Any]:
    if is_shutdown_pending():
        return Response(content={"status": "restarting"}, status_code=503)
    audit = run_audit(db=get_db())
    return Response(content={"status": "ok", "security": audit})


@get("/api/security-audit", sync_to_thread=False)
def security_audit(user: dict[str, Any]) -> Any:
    return run_audit(db=get_db())


@get("/api/listening-ports", sync_to_thread=False)
def listening_ports(user: dict[str, Any]) -> dict[str, Any]:
    return {"ports": list_listening_ports(db=get_db())}


@get("/api/storage-status", sync_to_thread=False)
def api_storage_status(user: dict[str, Any]) -> dict[str, Any]:
    return storage_status(get_config())


@post("/api/storage-guard", status_code=200)
async def toggle_storage_guard(
    data: Annotated[StorageGuardForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> dict[str, bool]:
    paused = (data.paused or "").strip().lower() in ("1", "true", "yes")
    set_guard_paused(paused)
    return {"guard_paused": is_guard_paused()}


@get("/api/ssh-status", sync_to_thread=False)
def ssh_status(user: dict[str, Any]) -> dict[str, bool]:
    return {"ssh_enabled": is_sshd_active()}


@post("/toggle-ssh", sync_to_thread=False, status_code=200)
def toggle_ssh(user: dict[str, Any]) -> dict[str, bool]:
    if is_sshd_active():
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "ssh.service", "sshd.service", "ssh.socket"],
            timeout=10,
        )
        subprocess.run(
            ["sudo", "-n", "systemctl", "mask", "ssh.service", "sshd.service", "ssh.socket"],
            timeout=10,
        )
        return {"ssh_enabled": False}
    subprocess.run(
        ["sudo", "-n", "systemctl", "unmask", "ssh.service", "sshd.service", "ssh.socket"],
        timeout=10,
    )
    subprocess.run(["sudo", "-n", "systemctl", "start", "ssh.service"], timeout=10)
    return {"ssh_enabled": is_sshd_active()}


@post("/api/drop-docker-cache", sync_to_thread=False, status_code=200)
def drop_docker_cache(user: dict[str, Any]) -> Response[dict[str, Any]]:
    try:
        output = drop_docker_build_cache()
    except RuntimeError as e:
        return Response(content={"ok": False, "error": str(e)}, status_code=500)
    return Response(content={"ok": True, "output": output})


@post("/restart_router", sync_to_thread=False, status_code=200)
def restart_router(user: dict[str, Any]) -> dict[str, bool]:
    subprocess.Popen(
        ["sudo", "-n", "bash", "-c", "systemctl kill --signal=SIGKILL openhost; systemctl start openhost"],
        start_new_session=True,
    )
    return {"ok": True}


# Public collection of route handlers for app.py registration.
api_system_routes = [
    ApiTokensController,
    compute_space_logs,
    health,
    security_audit,
    listening_ports,
    api_storage_status,
    toggle_storage_guard,
    ssh_status,
    toggle_ssh,
    drop_docker_cache,
    restart_router,
]
