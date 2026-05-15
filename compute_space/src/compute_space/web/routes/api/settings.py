from typing import Any

import attr
import git
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post

from compute_space.config import get_config
from compute_space.core.apps import inject_github_token_in_url
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


@attr.s(auto_attribs=True, frozen=True)
class GetRemoteResponse:
    ok: bool
    url: str | None = None
    ref: str | None = None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteRequest:
    url: str = ""


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteResponse:
    ok: bool
    token_applied: bool = False
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class HostPrep:
    host_prep_ok: bool
    container_runtime_available: bool
    host_prep_reason: str | None = None
    host_prep_message: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class CheckForUpdatesResponse:
    ok: bool
    state: str | None = None
    host_prep: HostPrep | None = None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class UpdateRepoStateResponse:
    ok: bool
    host_prep: HostPrep | None = None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


def _host_prep() -> HostPrep:
    """Whether the host is ready to run the installed router code."""
    runtime_ok = container_runtime_available()
    prep = host_prep_status()
    if not runtime_ok:
        return HostPrep(
            host_prep_ok=False,
            container_runtime_available=False,
            host_prep_reason="container_runtime_missing",
            host_prep_message=CONTAINER_RUNTIME_MISSING_ERROR,
        )
    if not prep.ok:
        return HostPrep(
            host_prep_ok=False,
            container_runtime_available=True,
            host_prep_reason=prep.reason,
            host_prep_message=prep.message,
        )
    return HostPrep(host_prep_ok=True, container_runtime_available=True)


@get("/api/settings/get_remote")
async def get_remote(user: dict[str, Any]) -> GetRemoteResponse:
    config = get_config()
    try:
        url = await get_remote_url(config.openhost_repo_path)
        ref = await get_current_ref(config.openhost_repo_path)
    except RemoteNotSetError:
        return GetRemoteResponse(ok=True)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as e:
        return GetRemoteResponse(ok=False, error=repr(e))
    return GetRemoteResponse(ok=True, url=url, ref=ref)


@post("/api/settings/set_remote", status_code=200)
async def set_remote(
    data: SetRemoteRequest,
    user: dict[str, Any],
) -> Response[SetRemoteResponse]:
    """Set git remote URL, injecting a GitHub auth token if available.

    A checkout is required so we can persist the ``ref`` setting properly,
    which means a whole reboot is needed afterwards.
    """
    config = get_config()
    url = (data.url or "").strip()
    if not url:
        return Response(content=SetRemoteResponse(ok=False, error="url is required"), status_code=400)

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
        return Response(content=SetRemoteResponse(ok=False, error=str(e)), status_code=500)
    return Response(content=SetRemoteResponse(ok=True, token_applied=token_applied))


@post("/api/settings/check_for_updates", status_code=200)
async def check_for_updates(user: dict[str, Any]) -> Response[CheckForUpdatesResponse]:
    config = get_config()
    try:
        state = await check_git_state(config.openhost_repo_path)
    except Exception as e:
        return Response(
            content=CheckForUpdatesResponse(ok=False, error=repr(e)),
            status_code=500,
        )
    return Response(
        content=CheckForUpdatesResponse(ok=True, state=str(state), host_prep=_host_prep()),
    )


@post("/api/settings/update_repo_state", status_code=200)
async def update_repo_state(user: dict[str, Any]) -> Response[UpdateRepoStateResponse]:
    """git reset to local origin/[branch] + pixi install.

    Returns HTTP 409 when the host isn't prepared for the installed runtime.
    """
    config = get_config()

    prep = _host_prep()
    if not prep.host_prep_ok:
        return Response(
            content=UpdateRepoStateResponse(ok=False, error=prep.host_prep_message, host_prep=prep),
            status_code=409,
        )

    ref = await get_current_ref(config.openhost_repo_path)
    try:
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        return Response(
            content=UpdateRepoStateResponse(ok=False, error=repr(e)),
            status_code=500,
        )
    return Response(content=UpdateRepoStateResponse(ok=True))


@post("/api/settings/restart_compute_space", status_code=200)
async def restart_compute_space(user: dict[str, Any]) -> OkResponse:
    trigger_restart()
    # this response may not get sent, don't depend on it
    return OkResponse(ok=True)


api_settings_routes = Router(
    path="/",
    route_handlers=[get_remote, set_remote, check_for_updates, update_repo_state, restart_compute_space],
)
