from quart import Blueprint
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.core.apps import inject_github_token_in_url
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
from compute_space.web.auth.middleware import login_required

api_settings_bp = Blueprint("api_settings", __name__)


@api_settings_bp.route("/api/settings/get_remote", methods=["GET"])
@login_required
async def get_remote() -> ResponseReturnValue:
    try:
        result = await agent_get_remote()
    except SystemAgentError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "url": result.url, "ref": result.ref})


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


@api_settings_bp.route("/api/settings/check_for_updates", methods=["POST"])
@login_required
async def check_for_updates() -> ResponseReturnValue:
    try:
        result = await agent_fetch()
    except SystemAgentError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    status = await _get_migration_status()
    payload: dict[str, object] = {"ok": True, "state": result.state, "host_prep_ok": status.ok}
    if not status.ok:
        payload["host_prep_reason"] = status.reason
        payload["host_prep_message"] = status.message
    return jsonify(payload)


@api_settings_bp.route("/api/settings/update_repo_state", methods=["POST"])
@login_required
async def update_repo_state() -> ResponseReturnValue:
    status = await _get_migration_status()
    if not status.ok:
        return jsonify(
            {
                "ok": False,
                "error": status.message,
                "host_prep_ok": False,
                "host_prep_reason": status.reason,
                "host_prep_message": status.message,
            }
        ), 409

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
