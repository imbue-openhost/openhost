import sqlite3

import attr
import bcrypt
import git
from litestar import Router
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException

from compute_space.config import Config
from compute_space.core.apps import inject_github_token_in_url
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.containers import container_runtime_available
from compute_space.core.git_ops import RemoteNotSetError
from compute_space.core.git_ops import get_current_ref
from compute_space.core.git_ops import get_remote_url
from compute_space.core.git_ops import init_repo_if_nonexistent
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.git_ops import set_remote_url
from compute_space.core.oauth import OAuthAuthorizationRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.core.runtime_sentinel import host_prep_status
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.updates import check_git_state
from compute_space.core.updates import hard_checkout_and_validate
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
class HostPrepOk:
    """Flat fields the dashboard reads when the host is ready for updates."""

    host_prep_ok: bool  # always True for this variant
    container_runtime_available: bool  # always True


@attr.s(auto_attribs=True, frozen=True)
class HostPrepBlocked:
    """Flat fields the dashboard reads when an update would brick the router.

    Carries the reason/message pair the banner renders verbatim.
    """

    host_prep_ok: bool  # always False for this variant
    container_runtime_available: bool
    host_prep_reason: str
    host_prep_message: str


@attr.s(auto_attribs=True, frozen=True)
class CheckUpdatesOk:
    state: str
    host_prep_ok: bool
    container_runtime_available: bool


@attr.s(auto_attribs=True, frozen=True)
class CheckUpdatesBlocked:
    state: str
    host_prep_ok: bool
    container_runtime_available: bool
    host_prep_reason: str
    host_prep_message: str


@attr.s(auto_attribs=True, frozen=True)
class OwnerUsernameResponse:
    username: str | None


def _host_prep_fields() -> HostPrepOk | HostPrepBlocked:
    """Probe the live runtime and the sentinel; return the shape the UI expects."""
    if not container_runtime_available():
        return HostPrepBlocked(
            host_prep_ok=False,
            container_runtime_available=False,
            host_prep_reason="container_runtime_missing",
            host_prep_message=CONTAINER_RUNTIME_MISSING_ERROR,
        )
    prep = host_prep_status()
    if not prep.ok:
        return HostPrepBlocked(
            host_prep_ok=False,
            container_runtime_available=True,
            host_prep_reason=prep.reason,
            host_prep_message=prep.message,
        )
    return HostPrepOk(host_prep_ok=True, container_runtime_available=True)


# --- routes -----------------------------------------------------------------


@get("/api/settings/get_remote", guards=[require_owner_auth])
async def get_remote(config: Config) -> GetRemoteResponse:
    try:
        url = await get_remote_url(config.openhost_repo_path)
        ref = await get_current_ref(config.openhost_repo_path)
    except RemoteNotSetError:
        return GetRemoteResponse()
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as e:
        raise HTTPException(detail=repr(e), status_code=500) from e
    return GetRemoteResponse(url=url, ref=ref)


@post("/api/settings/set_remote", status_code=200, guards=[require_owner_auth])
async def set_remote(data: SetRemoteRequest, config: Config) -> SetRemoteResponse:
    """Set git remote URL, injecting a GitHub auth token if available.

    A checkout is required so we can persist the ``ref`` setting properly,
    which means a whole reboot is needed afterwards.
    """
    url = (data.url or "").strip()
    if not url:
        raise HTTPException(detail="url is required", status_code=400)

    base_url, ref = parse_repo_url(url)
    ref = ref or "main"

    token_applied = False
    try:
        token = await get_oauth_token("github", ["repo"], return_to="/settings")
        base_url = inject_github_token_in_url(base_url, token)
        token_applied = True
    except (ServiceNotAvailable, OAuthAuthorizationRequired):
        pass  # best-effort; proceed without token

    try:
        await init_repo_if_nonexistent(config.openhost_repo_path)
        await set_remote_url(config.openhost_repo_path, base_url)
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        raise HTTPException(detail=str(e), status_code=500) from e
    return SetRemoteResponse(token_applied=token_applied)


@post("/api/settings/check_for_updates", status_code=200, guards=[require_owner_auth])
async def check_for_updates(config: Config) -> CheckUpdatesOk | CheckUpdatesBlocked:
    try:
        state = await check_git_state(config.openhost_repo_path)
    except Exception as e:
        raise HTTPException(detail=repr(e), status_code=500) from e
    prep = _host_prep_fields()
    if isinstance(prep, HostPrepBlocked):
        return CheckUpdatesBlocked(
            state=str(state),
            host_prep_ok=prep.host_prep_ok,
            container_runtime_available=prep.container_runtime_available,
            host_prep_reason=prep.host_prep_reason,
            host_prep_message=prep.host_prep_message,
        )
    return CheckUpdatesOk(
        state=str(state),
        host_prep_ok=prep.host_prep_ok,
        container_runtime_available=prep.container_runtime_available,
    )


@post("/api/settings/update_repo_state", status_code=204, guards=[require_owner_auth])
async def update_repo_state(config: Config) -> None:
    """git reset to local origin/[branch] + pixi install.

    Returns HTTP 409 when the host isn't prepared for the installed runtime;
    the blocking host_prep fields are carried in the exception's ``extra``.
    """
    prep = _host_prep_fields()
    if isinstance(prep, HostPrepBlocked):
        raise HTTPException(
            detail=prep.host_prep_message,
            status_code=409,
            extra=attr.asdict(prep),
        )

    ref = await get_current_ref(config.openhost_repo_path)
    try:
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        raise HTTPException(detail=repr(e), status_code=500) from e


@post("/api/settings/restart_compute_space", status_code=204, guards=[require_owner_auth])
async def restart_compute_space() -> None:
    # this response may not get sent, don't depend on it
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
    """Return the configured owner username for the dashboard form."""
    return OwnerUsernameResponse(username=read_owner_username(db))


@post("/api/settings/owner_username", status_code=200, guards=[require_owner_auth])
async def set_owner_username(data: SetOwnerUsernameRequest, db: sqlite3.Connection) -> OwnerUsernameResponse:
    """Update the owner's display username.

    Forwarded to per-app containers via ``OPENHOST_OWNER_USERNAME`` on their
    next reload; already-running containers keep the old value until they restart.
    """
    candidate = (data.username or "").strip()
    error = validate_owner_username(candidate)
    if error is not None:
        raise HTTPException(detail=error, status_code=400)

    try:
        update_owner_username(db, candidate)
        db.commit()
    except ValueError as e:
        # No user row yet — operator's next step is /setup, not retry.
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
