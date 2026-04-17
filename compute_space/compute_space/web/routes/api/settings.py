import git
from quart import Blueprint
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.apps import inject_github_token_in_url
from compute_space.core.git_ops import RemoteNotSetError
from compute_space.core.git_ops import get_current_ref
from compute_space.core.git_ops import get_remote_url
from compute_space.core.git_ops import init_repo_if_nonexistent
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.git_ops import set_remote_url
from compute_space.core.services import OAuthAuthorizationRequired
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_oauth_token
from compute_space.core.updates import check_git_state
from compute_space.core.updates import hard_checkout_and_validate
from compute_space.core.updates import trigger_restart
from compute_space.web.middleware import login_required

api_settings_bp = Blueprint("api_settings", __name__)


@api_settings_bp.route("/api/settings/get_remote", methods=["GET"])
@login_required
async def get_remote() -> ResponseReturnValue:
    config = get_config()
    try:
        url = await get_remote_url(config.openhost_repo_path)
        ref = await get_current_ref(config.openhost_repo_path)
    except RemoteNotSetError:
        return jsonify({"ok": True, "url": None, "ref": None})
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as e:
        return jsonify({"ok": False, "error": repr(e)}), 500
    return jsonify({"ok": True, "url": url, "ref": ref})


@api_settings_bp.route("/api/settings/set_remote", methods=["POST"])
@login_required
async def set_remote() -> ResponseReturnValue:
    """Set git remote URL, injecting a GitHub auth token if available.

    We have to do the checkout too, so that we can persist the `ref` setting properly.
    Which means a whole reboot is required.
    """
    config = get_config()
    data = await request.get_json()
    url = data.get("url", "").strip() if data else ""
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

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
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "token_applied": token_applied})


@api_settings_bp.route("/api/settings/check_for_updates", methods=["POST"])
@login_required
async def check_for_updates() -> ResponseReturnValue:
    config = get_config()
    try:
        state = await check_git_state(config.openhost_repo_path)
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500
    return jsonify({"ok": True, "state": str(state)})


@api_settings_bp.route("/api/settings/update_repo_state", methods=["POST"])
@login_required
async def update_repo_state() -> ResponseReturnValue:
    """git reset to local origin/[branch] + check that pixi install works."""
    config = get_config()
    ref = await get_current_ref(config.openhost_repo_path)
    try:
        await hard_checkout_and_validate(config.openhost_repo_path, ref)
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500
    return jsonify({"ok": True})


@api_settings_bp.route("/api/settings/restart_compute_space", methods=["POST"])
@login_required
async def restart_compute_space() -> ResponseReturnValue:
    trigger_restart()
    # this response may not get sent, don't depend on it
    return jsonify({"ok": True})
