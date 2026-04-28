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


def wait_for_app_running(url: str, token: str, app_name: str, timeout: float = 1800) -> None:
    """Block until the app reports ``status='running'`` or fails.

    Default 30-minute timeout caps the wait so a stuck deploy doesn't
    hang the CLI forever; large container images legitimately take a
    while to build, hence the generous default. Network errors are
    treated as transient (the server may be restarting mid-deploy)
    and retried within the overall timeout.
    """
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            print(
                f"Timed out waiting for {app_name} to reach 'running' after {timeout:.0f}s. "
                "Re-run 'oh app status' to check.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        time.sleep(3)
        try:
            resp = make_api_request(url, token, "GET", f"/api/app_status/{app_name}")
        except httpx.HTTPError:
            print("  (network error polling status; retrying)")
            continue
        try:
            result = resp.json()
        except ValueError:
            print("  (unparseable status response; retrying)")
            continue
        s = result.get("status", "unknown")
        if s == "running":
            print(f"{app_name} is running.")
            return
        if s == "error":
            print(f"{app_name} failed: {result.get('error', 'unknown error')}")
            raise SystemExit(1)
        print(f"  status: {s}...")


def wait_for_app_removed(url: str, token: str, app_name: str, timeout: float = 600) -> None:
    """Block until ``/api/app_status/<name>`` returns 404.

    /remove_app returns 202 immediately and runs the actual teardown in
    a background thread. The CLI must poll until the row is gone before
    claiming the removal is complete; otherwise the next ``oh app list``
    or re-deploy can race the worker.

    A 10-minute default timeout caps the wait so a stuck removal worker
    (e.g. ``deprovision_data`` blocked on a hung NFS mount) doesn't
    leave the CLI hanging forever — the operator gets a clear failure
    message and can investigate. Override via the ``timeout`` argument.
    """
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            print(
                f"Timed out waiting for {app_name} to finish removing after {timeout:.0f}s. "
                "The server may still be working — re-run 'oh app status' to check.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        time.sleep(2)
        try:
            resp = make_api_request(url, token, "GET", f"/api/app_status/{app_name}", raw=True)
        except httpx.HTTPError as e:
            # Transient network failure (ConnectError, TimeoutException,
            # etc.) during a server restart. Don't bail out — the server
            # may come back within the overall timeout. We print the
            # exception class to give the operator a clue without
            # dumping a full stack trace.
            print(f"  (network error polling status: {type(e).__name__}; retrying)")
            continue
        if resp.status_code == 404:
            return
        if resp.status_code >= 300:
            print(f"Error polling status: HTTP {resp.status_code}", file=sys.stderr)
            raise SystemExit(1)
        try:
            result = resp.json()
        except ValueError:
            # 2xx with a non-JSON body (e.g. an upstream proxy HTML
            # error page during a restart). Treat as transient and
            # keep polling — the row will either reappear with a
            # status or 404 once the server is back.
            print("  (unparseable status response; retrying)")
            continue
        s = result.get("status", "unknown")
        if s == "error":
            print(f"{app_name} removal failed: {result.get('error', 'unknown error')}", file=sys.stderr)
            raise SystemExit(1)
        print(f"  status: {s}...")
