import sqlite3

import bcrypt
import git
from quart import Blueprint
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
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
from compute_space.db import get_db
from compute_space.web.auth.middleware import login_required

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


def _host_prep_payload() -> dict[str, object]:
    """Return whether the host is ready to run the installed router code.

    Combines a live container-runtime probe (currently ``podman --version``)
    with the ``/etc/openhost/runtime`` sentinel.  Never raises.
    """
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
    config = get_config()
    try:
        state = await check_git_state(config.openhost_repo_path)
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500

    payload: dict[str, object] = {"ok": True, "state": str(state)}
    payload.update(_host_prep_payload())
    return jsonify(payload)


@api_settings_bp.route("/api/settings/update_repo_state", methods=["POST"])
@login_required
async def update_repo_state() -> ResponseReturnValue:
    """git reset to local origin/[branch] + pixi install.

    Returns HTTP 409 when the host isn't prepared for the installed
    runtime (the dashboard banner is the UI layer on top).
    """
    config = get_config()

    prep = _host_prep_payload()
    if not prep["host_prep_ok"]:
        payload: dict[str, object] = {"ok": False, "error": prep["host_prep_message"]}
        payload.update(prep)
        return jsonify(payload), 409

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


@api_settings_bp.route("/api/settings/change_password", methods=["POST"])
@login_required
async def change_password() -> ResponseReturnValue:
    data = await request.get_json()
    current = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    confirm = data.get("confirm_password", "")

    if not current or not new_pw:
        return jsonify({"ok": False, "error": "All fields required"}), 400
    if new_pw != confirm:
        return jsonify({"ok": False, "error": "Passwords do not match"}), 400
    if len(new_pw) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if not owner:
        return jsonify({"ok": False, "error": "No owner found"}), 404

    if not bcrypt.checkpw(current.encode(), owner["password_hash"].encode()):
        return jsonify({"ok": False, "error": "Current password is incorrect"}), 403

    new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE owner SET password_hash = ? WHERE id = 1", (new_hash,))
    db.commit()

    return jsonify({"ok": True})


@api_settings_bp.route("/api/settings/owner_username", methods=["GET"])
@login_required
async def get_owner_username() -> ResponseReturnValue:
    """Return the configured owner username for the dashboard form.

    Always returns 200 with the current value (or null if no owner
    row exists, which only happens during the brief window before
    /setup completes -- login_required would normally already reject
    in that state).
    """
    db = get_db()
    return jsonify({"ok": True, "username": read_owner_username(db)})


@api_settings_bp.route("/api/settings/owner_username", methods=["POST"])
@login_required
async def set_owner_username() -> ResponseReturnValue:
    """Update the owner's display username.

    The new value is forwarded to per-app containers via
    ``OPENHOST_OWNER_USERNAME`` on their next reload -- already-running
    containers keep the old value until they restart, which mirrors
    how every other ``OPENHOST_*`` env var is plumbed.  We don't
    bounce apps automatically here because that would surprise the
    operator; apps that haven't picked up the new value yet are
    surfaced as "stale" via the usual reload UI.

    Returns:
        - 400 with an operator-readable error on validation failure
          or pre-setup state (no owner row yet).
        - 500 with a structured error on DB failures (disk full,
          WAL lock timeout, etc.) -- matches the style of the other
          settings routes on the same page so the dashboard's
          generic error handler can render the message.
        - 200 with the new value on success.
    """
    data = await request.get_json()
    # ``data`` may be any JSON value (dict, list, scalar, ``None``);
    # only a JSON object can carry a ``username`` field.  Reject the
    # other shapes loudly with 400 rather than letting a non-mapping
    # ``data`` raise AttributeError on ``.get`` and surface as 500.
    if data is not None and not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    raw = (data or {}).get("username", "")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": "username must be a string"}), 400
    candidate = raw.strip()
    error = validate_owner_username(candidate)
    if error is not None:
        return jsonify({"ok": False, "error": error}), 400

    db = get_db()
    try:
        update_owner_username(db, candidate)
        db.commit()
    except ValueError as e:
        # ``update_owner_username`` raises ValueError when the owner
        # row is missing -- this is a 400, not a 500: the operator's
        # next action is "complete /setup", not "retry".
        return jsonify({"ok": False, "error": str(e)}), 400
    except sqlite3.Error as e:
        return jsonify({"ok": False, "error": f"database error: {e}"}), 500

    return jsonify({"ok": True, "username": candidate})
