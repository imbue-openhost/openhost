from __future__ import annotations

import sys
import time

import httpx


def make_api_request(
    domain: str,
    token: str,
    method: str,
    path: str,
    *,
    data: dict[str, str] | None = None,
    timeout: float = 120,
    raw: bool = False,
) -> httpx.Response:
    resp = httpx.request(
        method,
        f"{domain}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        data=data,
        timeout=timeout,
        follow_redirects=False,
    )
    if not raw and resp.status_code >= 300:
        try:
            body = resp.json()
            msg = body.get("error", body.get("message", resp.text))
        except Exception:
            msg = resp.text
        print(f"Error ({resp.status_code}): {msg}", file=sys.stderr)
        raise SystemExit(1)
    return resp


def wait_for_app_running(url: str, token: str, app_name: str) -> None:
    while True:
        time.sleep(3)
        result = make_api_request(url, token, "GET", f"/api/app_status/{app_name}").json()
        s = result.get("status", "unknown")
        if s == "running":
            print(f"{app_name} is running.")
            return
        if s == "error":
            print(f"{app_name} failed: {result.get('error', 'unknown error')}")
            raise SystemExit(1)
        print(f"  status: {s}...")
