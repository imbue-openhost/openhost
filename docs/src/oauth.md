# OAuth Token Service

The secrets app provides OAuth tokens to other apps via the cross-app service system. Apps request tokens by provider and scopes, and the secrets app handles the OAuth flow — either authorization code (Google) or device flow (GitHub), depending on the provider. Multiple accounts per provider are supported.

## How it works

### Requesting a token

1. **App requests a token:** `POST /_services/secrets/oauth/token` with `{"provider": "google", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"], "return_to": "//app.zone.domain/callback", "account": "user@gmail.com"}`

2. **Token cached?** Returns it immediately. Tokens that don't expire (GitHub) are returned as-is. Expiring tokens (Google) are checked with a 60s buffer.

3. **Token expired?** If a refresh token exists, refreshes automatically and returns the new access token.

4. **No token?** Returns `401` with `{"status": "authorization_required", "authorize_url": "..."}`. The app should redirect/popup the user to this URL. The `authorize_url` handles everything — permissions approval (if needed) and OAuth consent.

### Multiple accounts

Tokens are stored with an `account` label, keyed by `(provider, scopes, account)`. The account name is resolved automatically after OAuth — for Google it's the email address, for GitHub it's the username.

- **Connecting a new account:** Request a token with `"account": "new"`. After OAuth, the secrets app resolves the identity (e.g. `user@gmail.com`) and stores the token under that name.
- **Using a specific account:** Request with `"account": "user@gmail.com"` to get that account's token.
- **Default fallback:** Request with `"account": "default"` (or omit it). If there's exactly one token for that provider+scopes, it's returned regardless of account name.
- **Listing accounts:** `POST /_services/secrets/oauth/accounts` with `{"provider": "google", "scopes": [...]}` returns `{"accounts": ["user1@gmail.com", "user2@gmail.com"]}`.

### Authorization code flow (Google)

The `authorize_url` points directly to Google's consent page. After the user authorizes:

1. Google redirects to `https://my.<base_domain>/secrets/oauth/callback?code=...&state=...`
2. The `my.` provider redirect sends the user to `<zone_domain>/secrets/oauth/callback?code=...&state=...`
3. The router has an explicit route for `/secrets/oauth/callback` that proxies to the secrets app
4. Secrets app exchanges the code for tokens, fetches the user's identity (email), stores the token under that account name, and redirects to `return_to`

Google tokens always request `email` and `openid` scopes in addition to the requested scopes, so the identity can be resolved. The callback also verifies that all requested scopes were granted (Google's granular consent lets users uncheck scopes).

### Device flow (GitHub)

The `authorize_url` points to `secrets.<zone_domain>/oauth/device?...` — a page on the secrets app that:

1. Starts the device flow with GitHub, getting a user code and verification URL
2. Shows the user the code + a link to GitHub's verification page + a copy button
3. Polls GitHub in the background until the user authorizes
4. Fetches the user's identity (GitHub username), stores the token, and redirects to `return_to`

### Permission flow

Before an app can request OAuth tokens, it needs service permissions. Permission keys follow the format `oauth:<provider>:<scope>`.

When an app calls `/_services/secrets/oauth/token` without the needed permissions, the router handles it automatically:

1. Router checks `service_permissions` table → permissions not granted
2. Router auto-creates pending permission requests for the denied scopes
3. Router stores the original token request in an in-memory flow state
4. Router returns `401` with `authorize_url` pointing to `/approve-permissions?app=<app>&next=/_oauth/continue/<flow_id>`
5. User approves permissions on the approval page
6. Browser follows `next` to `/_oauth/continue/<flow_id>`, which proxies the original token request to the secrets app
7. Secrets app returns `401` with the OAuth `authorize_url` → router redirects to it
8. User completes OAuth → secrets app stores token → redirects to the app's `return_to`

This means the app only ever sees `200` (token) or `401` (single `authorize_url` that handles everything). The app never needs to call `/_services/request-permission` for OAuth.

The `/_services/request-permission` endpoint still exists and accepts a list of keys: `{"service": "secrets", "keys": [{"key": "oauth:google:scope", "reason": "..."}]}`.

### Browser auth (CORS)

The `/_services/` endpoints accept two forms of authentication:

- **Server-to-server:** `Authorization: Bearer <app_token>` header
- **Browser:** JWT auth cookie + `Origin` header. The router derives the app name from the Origin's subdomain (e.g. `oauth-demo.zone.domain` → app `oauth-demo`).

For browser requests, the router adds CORS headers allowing the app's subdomain origin with credentials. This lets client-side JavaScript call `/_services/` endpoints on the zone domain directly.

### Token storage

Tokens are stored in SQLite, keyed by `(provider, scopes, account)` where scopes is a sorted space-separated string. Different scope sets or accounts = different tokens.

### Token revocation

On deletion via the secrets dashboard:
- **Google:** revokes the refresh token via `https://oauth2.googleapis.com/revoke` (invalidates both access and refresh tokens)
- **GitHub:** revokes via `DELETE https://api.github.com/applications/{client_id}/token` with basic auth

## App integration patterns

### Server-side (redirects)

The app server requests the token. If auth is needed, it redirects the user's browser to the `authorize_url`. After OAuth, the user is redirected back to the original page (via `return_to`), which retries and gets the token.

```python
from oauth import get_oauth_token, OAuthAuthRequired

try:
    token = await get_oauth_token("google", [GMAIL_SCOPE], account,
                                   return_to="//app.zone.domain/unread")
except OAuthAuthRequired as e:
    return redirect(e.authorize_url)

# use token to call Google API server-side
```

### Client-side (popups)

JavaScript calls `/_services/` directly from the browser. If auth is needed, opens the `authorize_url` in a popup. The popup ends at `/oauth-complete` which sends `postMessage` back to the opener.

```javascript
var oauth = new OAuthClient({
    scopes: { google: ['https://...gmail.readonly'], github: ['repo'] },
    appName: 'my-app',
    zoneDomain: 'user.host.imbue.com',
});

// Get token (opens popup if needed, then retries)
var token = await oauth.getToken('google', 'user@gmail.com');

// Connect new account (opens popup, returns when done)
await oauth.connect('google');

// List connected accounts
var accounts = await oauth.getAccounts('google');
```

The `oauth.js` library handles the popup lifecycle, postMessage communication, and token retry. The `/oauth-complete` page sends `postMessage({ type: 'oauth_complete' })` to `window.opener` and closes itself.

## Providers

### Google (auth code flow)

- OAuth client type: **Web application**
- Created at https://console.cloud.google.com/apis/credentials
- Redirect URIs: `https://my.<base_domain>/secrets/oauth/callback`
- Enable needed APIs at https://console.cloud.google.com/apis/library
- No authorized JavaScript origins needed (server-side flow)
- Tokens expire (~1 hour) but have refresh tokens for automatic renewal
- Note: Google's device flow only supports a very limited set of scopes (no Gmail, Calendar, etc.), which is why we use the auth code flow

### GitHub (device flow)

- OAuth app created at https://github.com/settings/developers (or org settings)
- Enable "Device Flow" in the app settings
- No callback URL needed for the device flow
- Tokens don't expire and have no refresh token
- Device flow supports all scopes

## Adding a new provider

Add an entry to `PROVIDERS` in `apps/secrets_v1/src/secrets_v1/oauth.py` with either `"flow": "auth_code"` or `"flow": "device"`:

```python
# Auth code flow provider
"newprovider": {
    "flow": "auth_code",
    "client_id": "...",
    "client_secret": "...",
    "auth_url": "https://provider.com/oauth/authorize",
    "token_url": "https://provider.com/oauth/token",
    "revoke_url": "https://provider.com/oauth/revoke",  # or None
    "extra_auth_params": {},  # provider-specific params for the auth URL
},

# Device flow provider
"anotherprovider": {
    "flow": "device",
    "client_id": "...",
    "client_secret": "...",
    "device_code_url": "https://provider.com/login/device/code",
    "token_url": "https://provider.com/login/oauth/access_token",
    "revoke_url": None,
},
```

For auth code providers, register the redirect URI: `https://my.<base_domain>/secrets/oauth/callback`

For device flow providers, no redirect URI is needed.

To support identity resolution (account names), add a case in `fetch_account_identity()` in `oauth.py` that calls the provider's userinfo endpoint.

If the provider has a non-standard revocation API (like GitHub), add a case in the `revoke_token()` function in `oauth.py`.

## Files

- `apps/secrets_v1/src/secrets_v1/oauth.py` — provider configs, auth URL builder, code exchange, device flow, token refresh, revocation, identity resolution
- `apps/secrets_v1/src/secrets_v1/app.py` — `/_service/oauth/token` endpoint, `/_service/oauth/accounts` endpoint, `/oauth/callback` handler, `/oauth/device` page, dashboard APIs
- `apps/secrets_v1/src/secrets_v1/schema.sql` — `oauth_tokens` table (keyed by provider, scopes, account)
- `apps/secrets_v1/src/secrets_v1/templates/oauth_device.html` — device flow authorization page (shows code + verification link)
- `compute_space/compute_space/web/routes/services.py` — service proxy with permission checking, browser auth (CORS), auto-permission-request flow for OAuth, `/_oauth/continue` endpoint
- `apps/oauth_demo/` — example app with two demo modes:
  - `server_demo.py` — server-side OAuth with full-page redirects
  - `client_demo.py` — client-side SPA using `static/oauth.js` and popups
  - `oauth.py` — shared Python OAuth helpers (`get_oauth_token`, `get_accounts`)
  - `static/oauth.js` — client-side OAuth library (`OAuthClient` class)
