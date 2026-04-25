# Test Cases

Test cases for the OpenHost platform. Automated tests are in:
- `test_e2e.py` — cloud E2E (ephemeral GCE instance, real TLS)
- `test_full_stack.py` — local full-stack (router on host + rootless podman, no VM needed)
- `test_tls.py` — TLS cert acquisition (Pebble ACME + CoreDNS, no VM needed)
- `compute_space/tests/test_integration.py` — podman integration (router on host)

Legend: [x] = automated, [ ] = manual/not yet automated

## Provider

### Deploy & Setup Resilience
- [ ] `deploy.yml` does not overwrite provider DB
- [ ] `setup.yml` does not overwrite provider DB
- [ ] Running VMs survive a provider restart
- [ ] Running VMs survive a setup

### Admin
- [ ] First user is automatically admin
- [ ] Admin can view all users and VM statuses
- [ ] Admin can stop a running VM
- [ ] Admin can toggle admin status of other users
- [ ] Admin cannot remove their own admin status
- [ ] Admin can delete a user (stops VM, removes files, removes from DB)
- [ ] Admin can impersonate a user ("View as" -> sees their dashboard)
- [ ] Impersonation ends on logout

### User Accounts
- [ ] Signup via invite link creates user and sets auth cookies
- [x] Login with email/password sets auth cookies — `test_full_stack.py::TestLoginLogout`, `test_e2e.py::test_11b`
- [x] Expired access tokens are transparently refreshed — `test_integration.py::TestRouterCore::test_expired_token_refresh`
- [x] Logout revokes refresh token and clears cookies — `test_full_stack.py::TestLoginLogout`, `test_e2e.py::test_11c`
- [ ] Reserved usernames are rejected at signup

### VM Lifecycle
- [ ] VM boots and reaches "running" status
- [ ] VM port allocation is race-free (vm_ports table with UNIQUE constraint)
- [ ] VM ports don't collide between users
- [ ] Provider recovers VM states on restart
- [ ] Provider marks orphaned VMs as stopped

## Router (inside VM or standalone)

### Setup Flow
- [ ] Fresh VM: redirects to /setup
- [x] /setup inits DB, creates owner — `test_e2e.py::test_02`, `test_full_stack.py::admin_session`
- [x] /setup returns 403 if instance is already set up — `test_integration.py::TestRouterCore`
- [x] After /setup, all routes work normally — `test_e2e.py::test_03b`, `test_full_stack.py::TestRouter`

### Auth
- [x] /setup sets auth cookies (auto-login after setup) — `test_e2e.py::test_02`, `test_full_stack.py::TestRouter`
- [x] /dashboard requires authentication — `test_e2e.py::test_03`, `test_full_stack.py::TestRouter`
- [x] Unauthenticated requests redirect to /login — `test_e2e.py::test_03`, `test_full_stack.py::TestRouter`
- [ ] Claim token is validated against on-disk file
- [x] Claim token file is deleted after successful setup — `test_integration.py::test_claim_token_deleted_after_setup`
- [x] Login with wrong password rejected — `test_e2e.py::test_11`, `test_full_stack.py::TestLoginLogout`
- [x] Login with correct password sets cookies — `test_e2e.py::test_11b`, `test_full_stack.py::TestLoginLogout`
- [x] Logout clears session — `test_e2e.py::test_11c`, `test_full_stack.py::TestLoginLogout`

### API Tokens
- [x] Create API token — `test_e2e.py::test_10`, `test_full_stack.py::TestAPITokens`
- [x] Use token with Bearer header (no cookies) — `test_e2e.py::test_10b`, `test_full_stack.py::TestAPITokens`
- [x] Invalid token rejected — `test_e2e.py::test_10c`, `test_full_stack.py::TestAPITokens`
- [x] Delete token and verify invalidated — `test_e2e.py::test_10d`, `test_full_stack.py::TestAPITokens`
- [x] Token with no expiry works — `test_integration.py::test_api_token_no_expiry`

### TLS
- [x] TLS cert acquired via ACME (DNS-01 for wildcard) — `test_e2e.py::test_13`, `test_tls.py::TestCertAcquisition`
- [x] Wildcard cert covers app subdomains — `test_e2e.py::test_13b`, `test_tls.py::TestCertAcquisition::test_cert_covers_wildcard`
- [x] Cert uses ECDSA P-256 key — `test_tls.py::TestCertAcquisition::test_cert_uses_ecdsa_p256`
- [x] Cert and key are a matching pair — `test_tls.py::TestCertAcquisition::test_cert_key_match`
- [x] DNS TXT records cleaned up after acquisition — `test_tls.py::TestCertAcquisition::test_dns_txt_records_cleaned_up`
- [x] acquire_tls_cert writes files with correct permissions — `test_tls.py::TestAcquireTlsCert`
- [x] ACME account key round-trip (generate, save, load) — `test_tls.py::TestAccountKey`
- [ ] Router serves HTTPS in TLS mode — TODO: needs non-privileged Caddy port support
- [x] HTTP on :80 redirects to HTTPS (except ACME challenges) — `test_integration.py::test_caddyfile_http_redirect`

### Security (prod mode)
- [x] Pre-setup audit passes — `test_integration.py::test_pre_setup_security_audit`
- [x] Post-setup audit passes — `test_integration.py::TestRouterCore::test_post_setup_security_audit`
- [ ] SSH port not forwarded in prod mode
- [ ] SSH port forwarded in dev mode
- [x] Security audit endpoint returns results — `test_full_stack.py::TestStorageAndSystem`

### App Deployment
- [x] Deploy app from local path — `test_e2e.py::test_04`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Deploy app from Git URL — `test_integration.py::TestGitUrlDeployE2E`
- [ ] Deploy app from private GitHub repo (with token)
- [ ] Serverless app (Spin WASM) builds and starts
- [x] Container app builds and starts — `test_e2e.py::test_05`, `test_full_stack.py::TestTestAppPathRouting`
- [x] App reaches "running" status — `test_e2e.py::test_05`, `test_full_stack.py`
- [x] App removal stops process and cleans up — `test_e2e.py::test_14`, `test_integration.py::TestContainerE2E`

### App Lifecycle
- [x] Stop app — `test_e2e.py::test_08`, `test_full_stack.py::TestAppLifecycle`
- [x] Reload app (rebuild + restart) — `test_e2e.py::test_08b`, `test_full_stack.py::TestAppLifecycle`
- [x] Rename app, routing updates — `test_full_stack.py::TestAppRename`
- [x] Container engine restart recovery — `test_integration.py::TestContainerRestart`
- [x] Container gone recovery — `test_integration.py::TestContainerGone`
- [x] Remove with keep_data preserves persistent data — `test_integration.py::TestRemoveKeepData`
- [x] Git-deployed app: reload does git pull — `test_integration.py::TestGitUrlDeployE2E`

### App Routing (path-based)
- [x] Requests to /base_path/ are proxied to the app — `test_e2e.py::test_06b`, `test_full_stack.py::TestTestAppPathRouting`
- [x] App health check works through proxy — `test_e2e.py::test_06`, `test_full_stack.py::TestTestAppPathRouting`
- [x] POST requests proxied with body — `test_e2e.py::test_06c`, `test_full_stack.py::TestTestAppPathRouting`
- [x] X-Forwarded-* headers set, spoofed values stripped — `test_e2e.py::test_06d`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Unknown paths return 404 — `test_e2e.py::test_06e`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Unknown app paths return 404 — `test_e2e.py::test_06f`

### App Routing (subdomain-based)
- [x] Requests to app.zone.domain are proxied — `test_e2e.py::test_07`, `test_full_stack.py::TestTestAppSubdomainRouting`
- [x] Subdomain root returns app metadata — `test_e2e.py::test_07b`
- [x] Unauthenticated requests to non-public paths rejected — `test_e2e.py::test_07c`, `test_full_stack.py::TestTestAppSubdomainRouting`

### WebSocket Proxy
- [x] WebSocket echo via subdomain routing — `test_e2e.py::test_12d`, `test_full_stack.py::TestWebSocketProxy`
- [x] WebSocket echo via path-based routing — `test_full_stack.py::TestWebSocketProxy`

### Multiple Apps
- [x] Deploy multiple apps concurrently — `test_e2e.py::test_09`, `test_full_stack.py::TestMultipleApps`
- [x] Both apps route independently — `test_e2e.py::test_09b`, `test_full_stack.py::TestMultipleApps`
- [x] Subdomain isolation between apps — `test_e2e.py::test_09c`, `test_full_stack.py::TestMultipleApps`
- [x] GET /api/apps lists all apps — `test_e2e.py::test_09d`, `test_full_stack.py::TestMultipleApps`
- [x] Remove one app, other still works — `test_e2e.py::test_09e`, `test_full_stack.py::TestMultipleApps`

### Storage & System
- [x] GET /api/storage-status returns disk info — `test_e2e.py::test_12`, `test_full_stack.py::TestStorageAndSystem`
- [x] GET /app_logs returns log content — `test_e2e.py::test_12b`, `test_full_stack.py::TestStorageAndSystem`
- [x] GET /api/compute_space_logs returns router logs — `test_e2e.py::test_12c`, `test_full_stack.py::TestStorageAndSystem`

### SSH Toggle (dev mode)
- [x] GET /api/ssh-status returns state — `test_full_stack.py::TestSSHToggle`
- [x] POST /toggle-ssh toggles SSH — `test_full_stack.py::TestSSHToggle`

### App Data
- [x] App data persists across remove+redeploy (keep_data) — `test_integration.py::TestRemoveKeepData`
- [x] App data cleaned up on full remove — `test_integration.py::TestContainerE2E`
- [x] SQLite databases are provisioned and accessible — `test_integration.py::test_sqlite_provisioning`

## V2 Services (cross-app service proxy)

### Service Registration & Discovery
- [x] Deploying a provider app registers its V2 service — `test_full_stack.py::TestServicesV2::test_service_registered`
- [ ] GET /api/services_v2 lists all registered services
- [x] An app can provide multiple services (e.g. secrets + oauth) — `test_manifest.py::TestServicesV2Parsing::test_multiple_services_provides`
- [ ] Removing a provider app unregisters its services
- [ ] Re-deploying a provider app updates version/endpoint

### Version Resolution
- [x] Requesting an incompatible version returns 503 — `test_full_stack.py::TestServicesV2::test_version_mismatch_rejected`, `test_services_v2.py::TestVersionResolution::test_version_mismatch_raises`
- [x] Requesting a compatible version range (e.g. >=0.1.0) resolves correctly — `test_services_v2.py::TestVersionResolution::test_compatible_version_resolves`
- [x] Multiple providers for the same service: default provider is preferred — `test_services_v2.py::TestVersionResolution::test_default_provider_preferred`
- [x] Without default, highest compatible version is selected — `test_services_v2.py::TestVersionResolution::test_highest_version_without_default`
- [x] Provider exists but not running → specific error — `test_services_v2.py::TestVersionResolution::test_not_running_raises`
- [x] Invalid version specifier returns error — `test_services_v2.py::TestVersionResolutionEdgeCases::test_invalid_specifier_raises`
- [ ] Setting/removing a default provider via API

### V2 Permissions
- [x] Install-time permission grant (manifest `[[permissions_v2]]`) — `test_full_stack.py::TestServicesV2::test_install_time_grant_works`
- [x] Revoking a permission denies access — `test_full_stack.py::TestServicesV2::test_revoke_then_denied`, `test_services_v2.py::TestPermissionsV2::test_revoke`
- [x] Re-granting a permission restores access — `test_full_stack.py::TestServicesV2::test_regrant_then_works`
- [x] Ungranted keys are denied — `test_full_stack.py::TestServicesV2::test_ungranted_key_still_denied`
- [ ] Grant with wildcard key (`{"key": "*"}`) grants access to all keys
- [x] Permissions are scoped per-service (grant for secrets doesn't affect oauth) — `test_services_v2.py::TestPermissionsV2::test_permissions_scoped_per_service`
- [x] get_all_permissions_v2 lists and filters permissions — `test_services_v2.py::TestPermissionsV2::test_get_all_permissions`
- [x] Granting a duplicate permission is idempotent — `test_services_v2.py::TestPermissionsV2::test_grant_is_idempotent`
- [x] Multiple grants for same consumer+service tracked independently — `test_services_v2.py::TestPermissionsV2::test_multiple_grants_same_service`

### V2 Service Proxy
- [x] URL parsing: encoded service URL + endpoint extracted correctly — `test_services_v2.py::TestServiceUrlParsing`
- [ ] Consumer app auth via Bearer token works
- [ ] Consumer app auth via JWT cookie + Origin header works
- [ ] CORS preflight returns correct headers for app subdomains
- [ ] X-OpenHost-Permissions header is injected with granted permissions
- [ ] Provider returning 403 with required_grants is reformatted with approve URLs
- [ ] Request to non-existent service returns appropriate error
- [ ] Proxy forwards all HTTP methods (GET, POST, PUT, DELETE, PATCH)

### V1 Service Access Rules
- [x] OAuth token endpoint produces scoped permission keys — `test_services_v2.py::TestSecretsAccessRules::test_oauth_token_produces_scoped_permissions`
- [x] Get endpoint produces key-based permission keys — `test_services_v2.py::TestSecretsAccessRules::test_get_endpoint_produces_key_permissions`
- [x] Unknown endpoint on known service is denied — `test_services_v2.py::TestSecretsAccessRules::test_unknown_endpoint_denied`
- [x] Unknown service is denied — `test_services_v2.py::TestSecretsAccessRules::test_unknown_service_denied`

## Secrets Service

### Key-Value Secrets (V2)
- [x] Consumer app can fetch a granted secret key — `test_full_stack.py::TestServicesV2::test_install_time_grant_works`
- [x] Fetching an ungranted key returns 403 — `test_full_stack.py::TestServicesV2::test_ungranted_key_still_denied`
- [ ] Fetching a non-existent (but granted) key returns result with missing list
- [ ] Fetching multiple keys in one request returns all present values
- [ ] List endpoint returns key names without values

### Secrets Dashboard
- [ ] Owner can create/update/delete secrets via the dashboard API
- [ ] Import from env file parses export KEY=value lines

## OAuth Service

### Token Retrieval
- [x] No token exists → returns authorization redirect — `test_full_stack.py::TestOAuthFlow::test_no_token_returns_redirect`
- [x] Token exists → returns access token — `test_full_stack.py::TestOAuthFlow::test_get_token_first_account`
- [ ] Expired token with refresh token → refreshes and returns new token
- [ ] Expired token without refresh token → returns authorization redirect
- [ ] Requesting token for unknown provider returns 400

### Multi-Account Support
- [x] Single account: "default" resolves to that account — `test_full_stack.py::TestOAuthFlow::test_default_account_resolves`
- [x] Multiple accounts: "default" requires explicit selection — `test_full_stack.py::TestOAuthFlow::test_default_ambiguous_redirects`
- [x] Each account's token is retrievable by name — `test_full_stack.py::TestOAuthFlow::test_get_token_specific_account`
- [x] Accounts endpoint lists all connected accounts — `test_full_stack.py::TestOAuthFlow::test_accounts_shows_both`

### Authorization Flow
- [x] Auth code flow: authorize URL → callback → token stored — `test_full_stack.py::TestOAuthFlow::test_authorize_via_redirect_flow`
- [ ] Device flow: user code + verification URL → poll → token stored
- [ ] OAuth callback validates state parameter (rejects invalid/expired)
- [ ] OAuth callback verifies all requested scopes were granted
- [ ] Callback return_to is validated against zone domain (no open redirect)

### Provider Independence
- [x] Tokens for different providers are independent — `test_full_stack.py::TestOAuthFlow::test_different_provider_independent`
- [ ] Revoking a token for one provider doesn't affect another

### OAuth Permissions (V2)
- [ ] Consumer app with granted {provider, scope} can request tokens
- [ ] Consumer app without grant gets 403 with required_grants
- [ ] Install-time grant for OAuth service permissions
- [ ] Revoking OAuth permission blocks subsequent token requests

### OAuth Credential Management
- [ ] Dynamic-cred provider (Google): client_id/secret read from secrets DB
- [ ] Missing client credentials returns 503 with helpful message
- [ ] Token revocation calls provider's revoke endpoint

## DNS
- [ ] CoreDNS resolves base domain
- [ ] CoreDNS resolves zone subdomains
- [x] CoreDNS handles ACME DNS-01 challenges (prod mode) — `test_e2e.py::test_13` (implicit)
- [ ] DNS negative cache TTL is low enough for fast convergence

## Caddy (reverse proxy)
- [ ] TLS passthrough (SNI routing)
- [ ] HTTP reverse proxy
- [ ] Routes are updated when router starts/stops apps
- [ ] Routes are synced on router restart
