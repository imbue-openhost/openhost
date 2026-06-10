

## DNS

our imbue.com domain is managed at godaddy.
for each server we set up 2 DNS records:
- NS record pointing `host`(.imbue.com) to `ns.host`(.imbue.com). this says "the DNS server handling `host.` requests is at this location".
- "glue" A record pointing ns.host to the IP of the server. the NS record can't take an IP, so this resolves ns.host.imbue.com to the specific IP of the server.

on the server, we run CoreDNS (started by the router process). this serves authoritative DNS for the zone:
- A record for `host.imbue.com` -> server IP
- wildcard A record for `*.host.imbue.com` -> server IP (so app subdomains resolve)
- TXT records for `_acme-challenge.host.imbue.com` (written dynamically during ACME DNS-01 cert acquisition)

CoreDNS watches the zone file for SOA serial changes and auto-reloads. the router writes TXT records during cert acquisition and removes them afterward.

## TLS certs

we use wildcard certs: one cert covers both `host.imbue.com` and `*.host.imbue.com`. this means app subdomains get HTTPS without per-app cert issuance.

certs are acquired via ACME DNS-01 challenge:
1. router creates an ACME order for `[domain, *.domain]`
2. ACME server asks us to prove we control the domain by setting a TXT record
3. router writes the TXT record to the CoreDNS zone file
4. ACME server queries our CoreDNS, sees the TXT record, issues the cert
5. router clears the TXT record

the cert and key are stored on disk and reused across restarts. the router only acquires a new cert if none exists.

### cert providers

the DNS-01 issuance above is shared across both modes. what differs is *how the instance gets its ACME account key*, selected by the `cert_provider` config field:

**`eab_mint` (default, managed).** each instance creates its OWN ACME account:
- on first boot (no account key on disk yet), the router calls the central **cert-api** (`cert_api_url`, default = Imbue hosted) to mint a single-use [External Account Binding](https://datatracker.ietf.org/doc/html/rfc8555#section-7.3.4) credential.
- it generates its own ACME account key locally, registers a new account with Google Trust Services using that EAB, and persists the key to disk (`acme-account-key.json` under the openhost data dir).
- renewals reuse the persisted key and never call the cert-api again. a lost key just means a fresh EAB is minted on the next boot.
- the EAB only authorizes *creating one account* — it does not grant cert issuance. the instance still has to pass DNS-01, so it can only get certs for domains its own CoreDNS is authoritative for.
- this is the default for managed domains (e.g. `selfhost.imbue.com` subdomains). self-hosters can point `cert_api_url` at their own minter (see the `openhost-cert-api` repo).

this replaces the old model where every instance shipped a *shared* ACME account key via ansible — now the privileged account key is generated per-instance and never leaves the box.

**`byo` (bring-your-own).** the operator supplies their own ACME account key (`acme_account_key_path`) and CA (`acme_directory_url`). use this for non-managed domains where you bring your own account + CA. fully backward compatible — this is the pre-EAB-mint behavior. see "bring-your-own setup" below for how to create the key.

> **trust boundary.** the only thing stopping an instance from getting a cert for someone else's domain is DNS delegation: an instance is authoritative (via its CoreDNS) ONLY for its own delegated zone, so DNS-01 only succeeds for that zone. the router additionally refuses to *request* certs for anything outside its `zone_domain` (defense-in-depth — see `_assert_domains_within_zone`). follow-up: pin CAA `accounturi` records so the CA only honors orders from each instance's own account.

### bring-your-own setup (Google Trust Services)

the steps below create an ACME account key by hand for `byo` mode. they're also how the central cert-api is bootstrapped against GTS.

GTS is like let's encrypt but with higher rate limits tied to your GCP account (but still free).

https://docs.cloud.google.com/certificate-manager/docs/public-ca-tutorial
https://docs.cloud.google.com/certificate-manager/docs/quotas

prod server is at: https://dv.acme-v02.api.pki.goog/directory

brew install gcloud-cli

gcloud-init to genint project

`gcloud projects create openhost-tls-certs-1`
`gcloud config set project openhost-tls-certs-1`

`gcloud publicca external-account-keys create`

brew install certbot

if you have an existing account, `sudo rm /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog` to clear.

sudo certbot register \
    --email "me@example.com" \
    --no-eff-email \
    --server "https://dv.acme-v02.api.pki.goog/directory" \
    --eab-kid "(from previous step)" \
    --eab-hmac-key "(from previous step)"

it does not seem that the email becomes public. sudo is just needed because certbot writes its config to /etc/letsencrypt. this is the GCP prod keyserver.

grab the key from /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog/directory/[key id?]/private_key.json

for `byo` mode, point `acme_account_key_path` at this file (e.g. drop it in ansible secrets, kept in 1password). note this is the legacy *shared-key* path — managed instances use `eab_mint` and don't need it.

to revoke the keys, you have to delete the whole project. to reset rate limits, you can make a new project.

## reverse proxy

Caddy runs on the server alongside the router. it is started by the router process on boot.

when TLS is enabled:
- **:443** — terminates TLS using the ACME-acquired wildcard cert, reverse proxies all requests to the router on `:8080`
- **:80** — permanent redirect to HTTPS

when TLS is not enabled (e.g. Cloudflare Tunnel setups):
- **:80** — reverse proxies to the router on `:8080`

in dev mode (`openhost up --dev`), Caddy does not run at all — the router serves HTTP directly on `:8080`.

the Caddyfile is generated dynamically by `compute_space/compute_space/core/caddy.py`. no static Caddyfile is checked in.

## app routing

the router (Hypercorn on :8080) handles all app routing. two mechanisms:

1. **subdomain routing**: `my-app.host.imbue.com` — the router extracts `my-app` from the Host header and proxies to the app's container port.
2. **path prefix routing**: `host.imbue.com/my-app/...` — fallback when subdomains aren't available. the router strips the prefix before proxying.

both HTTP and WebSocket requests are proxied. auth (JWT cookie) is checked before proxying to non-public paths.

## latency

centralized, global web services do some things to get latency down:
- multiple servers around the world, with routing to get users to the closest one
- if they don't do that, they'll do something like have cloudflare terminate TLS at the edge and reverse proxy back to the origin server. this cuts down on roundtrips to negotiate TLS. but this lets cloudflare see all the traffic.


for a single server setup, there's some optimizations you can do:
- OCSP stapling: some clients will add a check that the cert isn't revoked before accepting it. OCSP stapling lets the server check the OCSP status itself and "staple" it to the TLS handshake, so the client doesn't have to do a separate request to the CA's OCSP server.
- TLS session resumption: after the first TLS handshake, the client and server can cache the session parameters. then on subsequent connections, they can do a shorter handshake that just references the cached session, which saves roundtrips. this is tricky because it is only properly secure on GET requests.
- TLS 1.3 has less roundtrips
- HTTP/3 has less roundtrips
- use fast ECDSA P-256 keys (we do this — see `compute_space/compute_space/core/tls/util.py`)
