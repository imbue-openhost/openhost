from typing import Any

import attr
import git
from litestar import Response
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
from compute_space.core.runtime_sentinel import host_prep_status
from compute_space.core.services import OAuthAuthorizationRequired
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_oauth_token
from compute_space.core.updates import check_git_state
from compute_space.core.updates import hard_checkout_and_validate
from compute_space.core.updates import trigger_restart


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteRequest:
    url: str = ""


@get("/api/settings/get_remote")
async def get_remote(user: dict[str, Any]) -> Response[dict[str, Any]]:
    config = get_config()
    try:
        url = await get_remote_url(config.openhost_repo_path)
        ref = await get_current_ref(config.openhost_repo_path)
    except RemoteNotSetError:
        return Response(content={"ok": True, "url": None, "ref": None})
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as e:
        return Response(content={"ok": False, "error": repr(e)}, status_code=500)
    return Response(content={"ok": True, "url": url, "ref": ref})


@post("/api/settings/set_remote", status_code=200)
async def set_remote(data: SetRemoteRequest, user: dict[str, Any]) -> Response[dict[str, Any]]:
    config = get_config()
    url = (data.url or "").strip()
    if not url:
        return Response(content={"ok": False, "error": "url is required"}, status_code=400)

    base_url, ref = parse_repo_url(url)
    ref = ref or "main"

    token_applied = False
    try:
        token = await get_oauth_token("github", ["repo"], return_to="/settings")
        base_url = inject_github_token_in_url(base_url, token)
        token_applied = True
    except (ServiceNotAvailable, OAuthAuthorizationRequired):
        pass

    try:
        await init_repo_if_nonexistent(config.openhost_repo_path)
        await set_remote_url(config.openhost_repo_path, base_url)
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        return Response(content={"ok": False, "error": str(e)}, status_code=500)
    return Response(content={"ok": True, "token_applied": token_applied})


def _host_prep_payload() -> dict[str, Any]:
    runtime_ok = container_runtime_available()
    prep = host_prep_status()
    payload: dict[str, Any] = {
        "host_prep_ok": runtime_ok and prep.ok,
        "container_runtime_available": runtime_ok,
    }
    if not runtime_ok:
        payload["host_prep_reason"] = "container_runtime_missing"
        payload["host_prep_message"] = CONTAINER_RUNTIME_MISSING_ERROR
    elif not prep.ok:
        payload["host_prep_reason"] = prep.reason
        payload["host_prep_message"] = prep.message
    return payload


@post("/api/settings/check_for_updates", status_code=200)
async def check_for_updates(user: dict[str, Any]) -> Response[dict[str, Any]]:
    config = get_config()
    try:
        state = await check_git_state(config.openhost_repo_path)
    except Exception as e:
        return Response(content={"ok": False, "error": repr(e)}, status_code=500)
    payload: dict[str, Any] = {"ok": True, "state": str(state)}
    payload.update(_host_prep_payload())
    return Response(content=payload)


@post("/api/settings/update_repo_state", status_code=200)
async def update_repo_state(user: dict[str, Any]) -> Response[dict[str, Any]]:
    config = get_config()
    prep = _host_prep_payload()
    if not prep["host_prep_ok"]:
        payload: dict[str, Any] = {"ok": False, "error": prep["host_prep_message"]}
        payload.update(prep)
        return Response(content=payload, status_code=409)

    ref = await get_current_ref(config.openhost_repo_path)
    try:
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        return Response(content={"ok": False, "error": repr(e)}, status_code=500)
    return Response(content={"ok": True})


@post("/api/settings/restart_compute_space", status_code=200)
async def restart_compute_space(user: dict[str, Any]) -> dict[str, bool]:
    trigger_restart()
    return {"ok": True}


api_settings_routes = [get_remote, set_remote, check_for_updates, update_repo_state, restart_compute_space]
