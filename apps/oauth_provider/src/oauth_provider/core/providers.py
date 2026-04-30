import asyncio
import json
import logging
import secrets
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

import oauth_provider.core.config as config

log = logging.getLogger(__name__)

PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "flow": "auth_code",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "github": {
        "flow": "device",
        "client_id": "Ov23liYd8LivfM50k6mn",  # gitleaks:allow
        "client_secret": "b7950ebeaf553f933eb1dfb826d4181104f24b63",  # gitleaks:allow
        "device_code_url": "https://github.com/login/device/code",
        "token_url": "https://github.com/login/oauth/access_token",
        "revoke_url": "https://api.github.com/applications/{client_id}/token",
        "revoke_method": "DELETE",
        "revoke_auth": "basic",
        "revoke_body": "json",
    },
    "mock": {
        "flow": "auth_code",
        "client_id": "dummy",
        "client_secret": "dummy",
        "auth_url": "dynamic",
        "token_url": "dynamic",
        "revoke_url": "dynamic",
    },
    "mock_device": {
        "flow": "device",
        "client_id": "dummy",
        "client_secret": "none",
        "device_code_url": "dummy",
        "token_url": "dynamic",
        "revoke_url": "dynamic",
    },
}

pending_auth_flows: dict[str, dict[str, Any]] = {}
active_device_flows: dict[str, dict[str, Any]] = {}

DEVICE_FLOW_TIMEOUT = 300


def normalize_scopes(scopes: list[str]) -> str:
    return ",".join(sorted(scopes))


# ─── Auth Code Flow ───


def build_auth_url(
    provider_name: str,
    scopes: list[str],
    redirect_uri: str,
    return_to: str,
    client_id: str,
    account: str = "default",
) -> str:
    provider = PROVIDERS[provider_name]
    state = json.dumps({"app": config.APP_NAME, "nonce": secrets.token_urlsafe(32)})

    pending_auth_flows[state] = {
        "provider": provider_name,
        "scopes": scopes,
        "redirect_uri": redirect_uri,
        "return_to": return_to,
        "account": account,
    }

    request_scopes = list(scopes)
    if provider_name == "google":
        for s in ("email", "openid"):
            if s not in request_scopes:
                request_scopes.append(s)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(request_scopes),
        "state": state,
        **provider.get("extra_auth_params", {}),
    }
    return f"{provider['auth_url']}?{urlencode(params)}"


async def exchange_code(
    provider_name: str, code: str, redirect_uri: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    provider = PROVIDERS[provider_name]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider["token_url"],
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            try:
                error_data = resp.json()
            except Exception:
                error_data = {}
            return {
                "error": error_data.get("error", "token_exchange_failed"),
                "error_description": error_data.get("error_description", resp.text),
            }
        result = resp.json()
        if result.get("error"):
            return {
                "error": result["error"],
                "error_description": result.get("error_description", ""),
            }
        expires_at = None
        if result.get("expires_in"):
            expires_at = (datetime.now(UTC) + timedelta(seconds=result["expires_in"])).isoformat()
        return {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
            "expires_at": expires_at,
            "token_type": result.get("token_type", "Bearer"),
        }


# ─── Device Flow ───


async def start_device_flow(provider_name: str, scopes: list[str]) -> dict[str, Any]:
    provider = PROVIDERS[provider_name]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider["device_code_url"],
            data={
                "client_id": provider["client_id"],
                "scope": " ".join(scopes),
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            try:
                error_data = resp.json()
            except Exception:
                error_data = {}
            return {
                "error": error_data.get("error", "request_failed"),
                "error_description": error_data.get("error_description", resp.text),
            }
        result: dict[str, Any] = resp.json()
        return result


def create_device_flow(
    provider_name: str,
    scopes: list[str],
    flow_data: dict[str, Any],
    account: str = "default",
    consumer_app: str = "",
) -> str:
    flow_id = secrets.token_urlsafe(16)
    flow = {
        "provider": provider_name,
        "scopes": scopes,
        "account": account,
        "consumer_app": consumer_app,
        "device_code": flow_data["device_code"],
        "user_code": flow_data["user_code"],
        "verification_url": flow_data.get("verification_uri", flow_data.get("verification_url", "")),
        "interval": flow_data.get("interval", 5),
        "status": "pending",
        "result": None,
        "started_at": time.time(),
    }
    active_device_flows[flow_id] = flow
    asyncio.ensure_future(_poll_device_flow(flow_id))
    return flow_id


async def _poll_device_flow(flow_id: str) -> None:
    flow = active_device_flows.get(flow_id)
    if not flow:
        return

    provider = PROVIDERS[flow["provider"]]
    deadline = flow["started_at"] + DEVICE_FLOW_TIMEOUT
    interval = flow["interval"]

    while time.time() < deadline:
        await asyncio.sleep(interval)

        if flow_id not in active_device_flows:
            return

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    provider["token_url"],
                    data={
                        "client_id": provider["client_id"],
                        "device_code": flow["device_code"],
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                )
                result = resp.json()
        except Exception:
            log.warning("Device flow poll error for %s, will retry", flow_id, exc_info=True)
            continue

        error = result.get("error")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
            flow["interval"] = interval
            continue
        elif error:
            flow["status"] = "error"
            flow["result"] = {
                "error": error,
                "message": result.get("error_description", error),
            }
            return

        expires_at = None
        if result.get("expires_in"):
            expires_at = (datetime.now(UTC) + timedelta(seconds=result["expires_in"])).isoformat()

        flow["status"] = "completed"
        flow["result"] = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
            "expires_at": expires_at,
            "token_type": result.get("token_type", "Bearer"),
        }
        return

    flow["status"] = "error"
    flow["result"] = {
        "error": "timeout",
        "message": "Device flow timed out waiting for authorization",
    }


# ─── Identity ───


USERINFO_URLS: dict[str, tuple[str, str]] = {
    "google": ("https://www.googleapis.com/oauth2/v2/userinfo", "email"),
    "github": ("https://api.github.com/user", "login"),
}


async def fetch_account_identity(provider_name: str, access_token: str) -> str | None:
    entry = USERINFO_URLS.get(provider_name)
    if not entry:
        return None
    url, field = entry
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                identity: str | None = data.get(field)
                return identity
    except Exception:
        log.warning("Failed to fetch identity for %s", provider_name, exc_info=True)
    return None


# ─── Token Operations ───


async def refresh_access_token(
    provider_name: str, refresh_token: str, client_id: str, client_secret: str
) -> dict[str, Any] | None:
    provider = PROVIDERS[provider_name]
    if not provider.get("token_url"):
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                provider["token_url"],
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            result: dict[str, Any] = resp.json()
            return result
    except Exception:
        log.warning("Failed to refresh %s token", provider_name, exc_info=True)
        return None


async def revoke_token(provider_name: str, token: str, client_id: str, client_secret: str) -> bool:
    provider = PROVIDERS[provider_name]
    revoke_url = provider.get("revoke_url")
    if not revoke_url:
        return True

    revoke_url = revoke_url.format(client_id=client_id)
    method = provider.get("revoke_method", "POST")
    auth = None
    if provider.get("revoke_auth") == "basic":
        auth = (client_id, client_secret)

    kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
    if provider.get("revoke_body") == "json":
        kwargs["json"] = {"access_token": token}
    else:
        kwargs["params"] = {"token": token}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(method, revoke_url, auth=auth, **kwargs)
            return resp.status_code in (200, 204)
    except Exception:
        log.warning("Failed to revoke %s token", provider_name, exc_info=True)
        return False
