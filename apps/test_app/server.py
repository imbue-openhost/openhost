"""Minimal HTTP + WebSocket server for testing app deployment.

Serves both HTTP routes and a WebSocket echo endpoint on the same port,
which is required because the OpenHost router proxies both HTTP and WS
traffic to the app's single port.

Routes:
    GET  /health         -> {"status": "ok"}
    GET  /               -> {"app": "test-app", "app_name": "..."}
    GET  /echo-headers   -> {"headers": {...}}
    POST <any>           -> {"method": "POST", "body": "...", "path": "..."}
    WS   /ws             -> echo: sends back whatever it receives
    *    <other>         -> 404 {"error": "not found"}
"""

import json
import os
from urllib.parse import quote

import aiohttp
import aiohttp.web

SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response({"status": "ok"})


async def handle_root(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response(
        {
            "app": "test-app",
            "app_name": os.environ.get("OPENHOST_APP_NAME", ""),
        }
    )


async def handle_echo_headers(request: aiohttp.web.Request) -> aiohttp.web.Response:
    headers = {k: v for k, v in request.headers.items()}
    return _json_response({"headers": headers})


async def handle_post(request: aiohttp.web.Request) -> aiohttp.web.Response:
    body = await request.text()
    return _json_response({"method": "POST", "body": body, "path": request.path})


async def handle_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """WebSocket echo endpoint: sends back whatever it receives."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws.send_bytes(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break
    return ws


async def handle_fetch_secret(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Fetch a secret from the secrets service via the V2 service proxy."""
    key = request.match_info["key"]
    version = request.query.get("version", ">=0.1.0")
    router_url = os.environ.get("OPENHOST_ROUTER_URL", "")
    app_token = os.environ.get("OPENHOST_APP_TOKEN", "")

    if not router_url or not app_token:
        return _json_response({"error": "OPENHOST_ROUTER_URL or OPENHOST_APP_TOKEN not set"}, status=500)

    encoded_svc = quote(SECRETS_SERVICE_URL, safe="")
    url = f"{router_url}/_services_v2/{encoded_svc}/get?version={quote(version, safe='')}"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"keys": [key]},
            headers={"Authorization": f"Bearer {app_token}"},
        ) as resp:
            body = await resp.json()
            return _json_response(body, status=resp.status)


async def handle_404(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response({"error": "not found"}, status=404)


def _json_response(data: dict, status: int = 200) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=json.dumps(data),
        content_type="application/json",
        status=status,
    )


def create_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)
    app.router.add_get("/echo-headers", handle_echo_headers)
    app.router.add_get("/fetch-secret/{key}", handle_fetch_secret)
    app.router.add_get("/ws", handle_ws)
    app.router.add_post("/{path:.*}", handle_post)
    # Catch-all for unknown GET paths -> 404
    app.router.add_get("/{path:.*}", handle_404)
    return app


if __name__ == "__main__":
    app = create_app()
    print("Test server listening on :5000", flush=True)
    aiohttp.web.run_app(app, host="0.0.0.0", port=5000, print=None)
