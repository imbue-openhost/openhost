from quart import Blueprint
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.core.apps import inject_github_token_in_url
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.containers import container_runtime_available
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.oauth import OAuthAuthorizationRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.core.runtime_sentinel import host_prep_status
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import agent_apply
from compute_space.core.system_agent import agent_fetch
from compute_space.core.system_agent import agent_get_remote
from compute_space.core.system_agent import agent_set_remote
from compute_space.core.updates import trigger_restart
from compute_space.web.auth.middleware import login_required

api_settings_bp = Blueprint("api_settings", __name__)


@api_settings_bp.route("/api/settings/get_remote", methods=["GET"])
@login_required
async def get_remote() -> ResponseReturnValue:
    try:
        result = await agent_get_remote()
    except SystemAgentError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "url": result.get("url"), "ref": result.get("ref")})


@api_settings_bp.route("/api/settings/set_remote", methods=["POST"])
@login_required
async def set_remote() -> ResponseReturnValue:
    data = await request.get_json()
    url = data.get("url", "").strip() if data else ""
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

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
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "token_applied": token_applied})


def _host_prep_payload() -> dict[str, object]:
    runtime_ok = container_runtime_available()
    prep = host_prep_status()
    payload: dict[str, object] = {
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


@api_settings_bp.route("/api/settings/check_for_updates", methods=["POST"])
@login_required
async def check_for_updates() -> ResponseReturnValue:
    try:
        result = await agent_fetch()
    except SystemAgentError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    payload: dict[str, object] = {"ok": True, "state": result.get("state", "UNKNOWN")}
    payload.update(_host_prep_payload())
    return jsonify(payload)


@api_settings_bp.route("/api/settings/update_repo_state", methods=["POST"])
@login_required
async def update_repo_state() -> ResponseReturnValue:
    prep = _host_prep_payload()
    if not prep["host_prep_ok"]:
        payload: dict[str, object] = {"ok": False, "error": prep["host_prep_message"]}
        payload.update(prep)
        return jsonify(payload), 409

    try:
        await agent_apply()
    except SystemAgentError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@api_settings_bp.route("/api/settings/restart_compute_space", methods=["POST"])
@login_required
async def restart_compute_space() -> ResponseReturnValue:
    trigger_restart()
    return jsonify({"ok": True})
