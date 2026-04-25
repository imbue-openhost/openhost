import json
import re
from contextlib import closing

from db import get_db
from db import init_db
from quart import Quart
from quart import jsonify
from quart import request

app = Quart(__name__)

init_db()


# ─── Owner Dashboard ───


@app.route("/")
async def index():
    from quart import render_template  # noqa: PLC0415

    with closing(get_db()) as db:
        secrets = db.execute("SELECT * FROM secrets ORDER BY key").fetchall()
    return await render_template("index.html", secrets=secrets)


@app.route("/api/secrets", methods=["GET"])
async def list_secrets():
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description, created_at, updated_at FROM secrets ORDER BY key").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/secrets", methods=["POST"])
async def set_secret():
    data = await request.get_json()
    if not data or not data.get("key") or "value" not in data:
        return jsonify({"error": "key and value are required"}), 400
    with closing(get_db()) as db:
        db.execute(
            """INSERT INTO secrets (key, value, description)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   description = excluded.description,
                   updated_at = datetime('now')""",
            (data["key"], data["value"], data.get("description", "")),
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/secrets/<key>", methods=["DELETE"])
async def delete_secret(key):
    with closing(get_db()) as db:
        db.execute("DELETE FROM secrets WHERE key = ?", (key,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/import", methods=["POST"])
async def import_secrets():
    """Import secrets from a shell-style env file.

    Parses lines like:
        export KEY=value
        export KEY="value"
        export KEY='value'
        KEY=value
    Skips comments (#) and blank lines. Upserts all parsed key-value pairs.
    """
    data = await request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "content is required"}), 400

    parsed = _parse_env_file(data["content"])
    description = data.get("description", "")

    with closing(get_db()) as db:
        imported = 0
        skipped = 0
        for key, value in parsed:
            if not value:
                skipped += 1
                continue
            db.execute(
                """INSERT INTO secrets (key, value, description)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = datetime('now')""",
                (key, value, description),
            )
            imported += 1
        db.commit()
    return jsonify({"ok": True, "imported": imported, "skipped": skipped})


def _parse_env_file(content):
    """Parse shell-style env file. Returns list of (key, value) tuples."""
    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not m:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        results.append((key, value))
    return results


# ─── Service API (called by other apps via router) ───


@app.route("/_service/get", methods=["POST"])
async def service_get():
    """Return secret values for the requested keys."""
    data = await request.get_json()
    requested_keys = data.get("keys", []) if data else []

    if not requested_keys:
        return jsonify({"error": "No keys requested"}), 400

    with closing(get_db()) as db:
        result = {}
        for key in requested_keys:
            row = db.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
            if row:
                result[key] = row["value"]

    missing = [k for k in requested_keys if k not in result]
    response = {"secrets": result}
    if missing:
        response["missing"] = missing
        response["message"] = (
            f"The following secrets are not set: {', '.join(missing)}. Add them in the secrets app dashboard."
        )
    return jsonify(response)


@app.route("/_service/list", methods=["GET"])
async def service_list():
    """List available secret keys (names only, not values)."""
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description FROM secrets ORDER BY key").fetchall()
    return jsonify({"keys": [{"key": r["key"], "description": r["description"]} for r in rows]})


# ─── V2 Service API (secrets — permissions validated by provider) ───


def _parse_v2_grants() -> tuple[set[str], bool]:
    """Read X-OpenHost-Permissions header and return (granted_keys, grant_all)."""
    perms_header = request.headers.get("X-OpenHost-Permissions", "[]")
    try:
        grants = json.loads(perms_header)
    except json.JSONDecodeError:
        return set(), False

    granted_keys: set[str] = set()
    for g in grants:
        payload = g.get("grant", {})
        if isinstance(payload, dict):
            if payload.get("key") == "*":
                return set(), True
            if "key" in payload:
                granted_keys.add(payload["key"])
    return granted_keys, False


@app.route("/_service_v2/get", methods=["POST"])
async def service_v2_get():
    """Return secret values for the requested keys (V2: provider-side permission check)."""
    data = await request.get_json()
    requested_keys = data.get("keys", []) if data else []

    if not requested_keys:
        return jsonify({"error": "No keys requested"}), 400

    granted_keys, grant_all = _parse_v2_grants()
    if not grant_all:
        missing_perms = [k for k in requested_keys if k not in granted_keys]
        if missing_perms:
            return jsonify(
                {
                    "error": "permission_required",
                    "required_grant": {
                        "grant_payload": {"key": missing_perms[0]},
                    },
                }
            ), 403

    with closing(get_db()) as db:
        result = {}
        for key in requested_keys:
            row = db.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
            if row:
                result[key] = row["value"]

    missing = [k for k in requested_keys if k not in result]
    response = {"secrets": result}
    if missing:
        response["missing"] = missing
    return jsonify(response)


@app.route("/_service_v2/list", methods=["GET"])
async def service_v2_list():
    """List available secret keys (V2: no permission check needed for names)."""
    with closing(get_db()) as db:
        rows = db.execute("SELECT key, description FROM secrets ORDER BY key").fetchall()
    return jsonify({"keys": [{"key": r["key"], "description": r["description"]} for r in rows]})
