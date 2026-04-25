import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

SPEC_PATH = Path(__file__).resolve().parents[3] / "services" / "oauth" / "openapi.yaml"
SVC_PREFIX = "/oauth_service"


def _filter_service_paths(schema: dict) -> dict:
    filtered = dict(schema)
    new_paths = {}
    for path, ops in schema.get("paths", {}).items():
        if path.startswith(SVC_PREFIX):
            new_paths[path[len(SVC_PREFIX) :]] = ops
    filtered["paths"] = new_paths
    return filtered


def test_no_breaking_changes_vs_spec(oauth_app_url: str) -> None:
    resp = httpx.get(f"{oauth_app_url}/schema/openapi.json")
    assert resp.status_code == 200, f"Failed to fetch schema: {resp.status_code}"

    filtered = _filter_service_paths(resp.json())

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(filtered, f)
        generated_path = f.name

    try:
        result = subprocess.run(
            ["oasdiff", "breaking", str(SPEC_PATH), generated_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"oasdiff found breaking changes:\n{result.stdout}\n{result.stderr}"
    finally:
        os.unlink(generated_path)
