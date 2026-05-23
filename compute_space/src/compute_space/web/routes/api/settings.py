import sqlite3

import attr
import bcrypt
from litestar import Router
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException

from compute_space.core.apps import inject_github_token_in_url
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.oauth import OAuthAuthorizationRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.system_agent import MigrationStatus
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import agent_apply
from compute_space.core.system_agent import agent_fetch
from compute_space.core.system_agent import agent_get_remote
from compute_space.core.system_agent import agent_set_remote
from compute_space.core.system_agent import agent_status
from compute_space.core.updates import trigger_restart
from compute_space.web.auth.auth import require_owner_auth

# --- request / response types -----------------------------------------------


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteRequest:
    url: str = ""


@attr.s(auto_attribs=True, frozen=True)
class SetOwnerUsernameRequest:
    username: str = ""


@attr.s(auto_attribs=True, frozen=True)
class GetRemoteResponse:
    url: str | None = None
    ref: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteResponse:
    token_applied: bool


@attr.s(auto_attribs=True, frozen=True)
class CheckUpdatesOk:
    state: str
    host_prep_ok: bool


@attr.s(auto_attribs=True, frozen=True)
class CheckUpdatesBlocked:
    state: str
    host_prep_ok: bool
    host_prep_reason: str
    host_prep_message: str


@attr.s(auto_attribs=True, frozen=True)
class OwnerUsernameResponse:
    username: str | None


async def _get_migration_status() -> MigrationStatus:
    try:
        return await agent_status()
    except SystemAgentError:
        return MigrationStatus(
            ok=False,
            reason="agent_error",
            message="System agent unavailable",
            current_version=0,
            expected_version=0,
        )


# --- routes -----------------------------------------------------------------


@get("/api/settings/get_remote", guards=[require_owner_auth])
async def get_remote() -> GetRemoteResponse:
    try:
        result = await agent_get_remote()
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e
    return GetRemoteResponse(url=result.url, ref=result.ref)


@post("/api/settings/set_remote", status_code=200, guards=[require_owner_auth])
async def set_remote(data: SetRemoteRequest) -> SetRemoteResponse:
    url = (data.url or "").strip()
    if not url:
        raise HTTPException(detail="url is required", status_code=400)

    base_url, _ref = parse_repo_url(url)

    token_applied = False
    try:
        token = await get_oauth_token("github", ["repo"], return_to="/settings")
        base_url = inject_github_token_in_url(base_url, token)
        token_applied = True
    except (ServiceNotAvailable, OAuthAuthorizationRequired):
        pass

    try:
        await agent_set_remote(base_url)
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e
    return SetRemoteResponse(token_applied=token_applied)


@post("/api/settings/check_for_updates", status_code=200, guards=[require_owner_auth])
async def check_for_updates() -> CheckUpdatesOk | CheckUpdatesBlocked:
    try:
        result = await agent_fetch()
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e

    status = await _get_migration_status()
    if not status.ok:
        return CheckUpdatesBlocked(
            state=result.state,
            host_prep_ok=False,
            host_prep_reason=status.reason,
            host_prep_message=status.message,
        )
    return CheckUpdatesOk(state=result.state, host_prep_ok=True)


@post("/api/settings/update_repo_state", status_code=204, guards=[require_owner_auth])
async def update_repo_state() -> None:
    status = await _get_migration_status()
    if not status.ok:
        raise HTTPException(
            detail=status.message,
            status_code=409,
            extra={"host_prep_ok": False, "host_prep_reason": status.reason, "host_prep_message": status.message},
        )

    try:
        await agent_apply()
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e


@post("/api/settings/restart_compute_space", status_code=204, guards=[require_owner_auth])
async def restart_compute_space() -> None:
    trigger_restart()


@attr.s(auto_attribs=True, frozen=True)
class ChangePasswordRequest:
    current_password: str = ""
    new_password: str = ""
    confirm_password: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ChangePasswordResponse:
    ok: bool = True


@post("/api/settings/change_password", status_code=200, guards=[require_owner_auth])
async def change_password(data: ChangePasswordRequest, db: sqlite3.Connection) -> ChangePasswordResponse:
    current = (data.current_password or "").strip()
    new_pw = (data.new_password or "").strip()
    confirm = (data.confirm_password or "").strip()

    if not current or not new_pw:
        raise HTTPException(detail="All fields required", status_code=400)
    if new_pw != confirm:
        raise HTTPException(detail="Passwords do not match", status_code=400)
    if len(new_pw) < 8:
        raise HTTPException(detail="Password must be at least 8 characters", status_code=400)

    row = db.execute("SELECT user_id, password_hash FROM users LIMIT 1").fetchone()
    if not row:
        raise HTTPException(detail="No owner found", status_code=404)

    if not bcrypt.checkpw(current.encode(), row["password_hash"].encode()):
        raise HTTPException(detail="Current password is incorrect", status_code=403)

    new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE user_id = ?",
        (new_hash, row["user_id"]),
    )
    db.commit()

    return ChangePasswordResponse(ok=True)


@get("/api/settings/owner_username", guards=[require_owner_auth])
async def get_owner_username(db: sqlite3.Connection) -> OwnerUsernameResponse:
    return OwnerUsernameResponse(username=read_owner_username(db))


@post("/api/settings/owner_username", status_code=200, guards=[require_owner_auth])
async def set_owner_username(data: SetOwnerUsernameRequest, db: sqlite3.Connection) -> OwnerUsernameResponse:
    candidate = (data.username or "").strip()
    error = validate_owner_username(candidate)
    if error is not None:
        raise HTTPException(detail=error, status_code=400)

    try:
        update_owner_username(db, candidate)
        db.commit()
    except ValueError as e:
        raise HTTPException(detail=str(e), status_code=400) from e
    except sqlite3.Error as e:
        raise HTTPException(detail=f"database error: {e}", status_code=500) from e

    return OwnerUsernameResponse(username=candidate)


api_settings_routes = Router(
    path="/",
    route_handlers=[
        get_remote,
        set_remote,
        check_for_updates,
        update_repo_state,
        restart_compute_space,
        change_password,
        get_owner_username,
        set_owner_username,
    ],
)
