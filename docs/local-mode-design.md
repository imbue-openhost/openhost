# Local (mDNS) mode + concurrent multi-domain serving — design & plan

> **Status:** living design doc. Planning only — no code written yet.
> This will be updated as decisions are made and work lands.
> Last updated: 2026-07-21.

## Goal

Make **local running a first-class mode**, discoverable on the LAN via **mDNS
(`.local`)**, with a **provisioning script** that sets an instance up for local
use. Crucially, an instance must be able to answer on **multiple domains at the
same time** and must **never force-redirect to a single "main" domain**: a
plain-HTTP `.local` request and an HTTPS external request must both work
concurrently against the same instance. There may still be a canonical/primary
domain, but secondary domains (including `.local`) are always supported.

## Requirements (from the original ask)

1. Local mode is first-class, not just a `--dev` afterthought.
2. Uses mDNS for `.local` discovery.
3. A provisioning script sets an instance up for local use.
4. The instance always supports multiple domains accessing it simultaneously.
5. No always-redirect to the main domain; secondary domains are always honored.
6. HTTP `.local` and HTTPS external can run at the same time.

---

## Where the code is today (research findings)

The current architecture is built around **one** `zone_domain` (a scalar,
`config.py:30`) and **one** global scheme (`config.tls_enabled`). Those two
globals fan out into everything:

- **DNS:** the router runs its own authoritative **CoreDNS**, serving a
  wildcard `* IN A {public_ip}` for the zone (`core/dns.py`,
  `core/templates/zonefile:13`). CoreDNS is also used for ACME DNS-01
  challenges.
- **TLS / Caddy:** the Caddyfile is **generated dynamically by the router** at
  startup (`core/caddy.py:10`), not templated by ansible. It's an all-or-nothing
  TLS toggle. The **single** HTTP→HTTPS redirect lives here — a `:80 { redir
  https://{host}{uri} permanent }` block emitted only when `tls_enabled`
  (`core/caddy.py:23-25`). Caddy terminates TLS on `:443` and reverse-proxies
  plain HTTP to the router on loopback `:8080`.
- **Routing:** strictly **subdomain/host-based**, no path routing for
  user-facing apps. `get_app_from_hostname` (`core/apps.py:1012-1033`) strips the
  `.{zone_domain}` suffix to get a single-label app name;
  `SubdomainProxyMiddleware` (`web/middleware/subdomain_proxy.py`) proxies to the
  app's `local_port`.
- **Scheme is global, not per-request.** Because Caddy terminates TLS and speaks
  HTTP on loopback, the router never sees the real scheme; it derives
  `X-Forwarded-Proto` from `config.tls_enabled` (`subdomain_proxy.py:166`).
- **Redirect-to-canonical:** an unauthenticated app-subdomain request bounces to
  `{proto}://{zone_domain}/login?next=…` via `build_login_url`
  (`web/auth/auth.py:190-196`). This is the one place traffic is forced to the
  single canonical domain.
- **Cookies:** the session cookie is scoped to `zone_domain_no_port`
  (`web/auth/cookies.py`, `web/routes/pages/login.py:62`), so one session covers
  all `*.zone_domain` app subdomains. `secure=` comes from the global
  `tls_enabled`.
- **Config loading:** typed-settings over a TOML file (`config.py:287`), path
  from `OPENHOST_ROUTER_CONFIG`. Local `openhost up` writes
  `~/.openhost/local_compute_space/config.toml` via
  `routerd_cli/.../config_gen.py` (forces `tls_enabled=False`,
  `start_caddy=False`). Server deploys render `ansible/templates/config.toml.j2`;
  there's already a `local_http_only` escape hatch that turns TLS/CoreDNS/Caddy
  all off.
- **Provisioning:** `scripts/provision.sh` (curl-pipe-bash bootstrap) wraps
  `ansible/local_setup.yml`; `ansible/setup.yml`/`deploy.yml` for remote. Already
  has a `--local-http-only` flag.
- **No mDNS anywhere.** Zero hits for `mdns`/`avahi`/`bonjour`/`zeroconf` in the
  source. The `.local` strings that exist are just test zone domains.

**Bottom line:** "multiple domains, http-`.local` + https-external, no forced
redirect" is fundamentally **one refactor** — stop reading the two globals and
instead resolve *per request* which domain the request arrived on. Plus one new
subsystem: a wildcard **mDNS responder**.

---

## Conceptual model (the de-complecting)

Introduce a first-class **Domain**:

```
Domain = { name, tls: bool, discovery: "public_dns" | "mdns" }
```

An instance answers on a **set** of Domains. One is the **primary** (canonical —
used for outbound links like emails/OAuth and for background tasks that have no
request in hand). Everything else is resolved **per request** from the Host
header:

- `myapp.myhost.local` → Domain `myhost.local` (tls=false, mdns) → app `myapp`,
  scheme `http`, links/cookies stay on `.local`.
- `myapp.myhost.example.com` → Domain `myhost.example.com` (tls=true) → app
  `myapp`, scheme `https`, links/cookies stay on the public domain.

`zone_domain` / `tls_enabled` don't disappear — they collapse into **"the
primary Domain,"** read only where there is no request (cert renewal, DNS
zonefile generation, the canonical `OPENHOST_ZONE_DOMAIN` env var handed to
apps). Existing single-domain deployments keep working unchanged because the
primary Domain is synthesized from today's `zone_domain` + `tls_enabled`.

This is the key separation: today "which domain" and "which scheme" are two
conflated globals; we split them into a **per-request resolved Domain**, so
routing, scheme, links, and cookies all hang off the domain the request actually
came in on.

---

## The mDNS wildcard decision

### Why standard daemons "can't" wildcard — and why a custom responder can

mDNS the *protocol* has no wildcard limitation. Every mDNS query is multicast to
`224.0.0.251:5353` (and `ff02::fb` for v6) and seen by every responder on the
link. The limitation is a **policy of the standard daemons**: Avahi/Bonjour only
answer for names they've explicitly published (their own hostname + registered
records), so they won't reply to an arbitrary `foo.myhost.local`.

A **custom responder** can match `*.{zone}.local` against a pattern and answer
with the host's LAN IP. This mirrors exactly what OpenHost already does with
DNS: it doesn't rely on the system resolver, it **runs its own authoritative
CoreDNS** serving `* IN A {ip}`. The mDNS story becomes the same shape: run our
own authoritative mDNS responder that serves the wildcard.

### Decision: custom wildcard mDNS responder (not per-app publishing)

New subsystem `core/mdns.py`: a wildcard responder (raw UDP socket, join the
mDNS multicast group, parse DNS wire-format queries, answer any QNAME matching
`{zone}.local` or `*.{zone}.local` with an A/AAAA record → LAN IP; honor the QU
unicast-response bit). Managed as a subprocess/thread with the same
`CoreDNSProcess`/`CaddyProcess` pattern already in the tree.

Consequences (all positive):

- **Subdomain routing stays fully intact** — `{app}.{zone}.local` all resolve,
  so the routing/URL/cookie layers need no `.local`-specific special-casing; the
  `.local` Domain is just another entry in the Domain set.
- **No app-lifecycle hooks.** New apps are instantly resolvable the moment they
  deploy, because the responder answers the whole `*.{zone}.local` space — same
  as CoreDNS does for the public zone today. (This deletes the per-app
  publish/unpublish plumbing an Avahi-publish approach would have needed.)
- **No Avahi dependency.** Provisioning does *not* install/run `avahi-daemon`;
  our responder is the sole mDNS authority on the box, so there's no fight over
  port 5353.

### Rejected alternative: single `.local` name + path routing

`myhost.local/apps/myapp/`. Avoids wildcard, but requires a brand-new
path-routing branch in the proxy *and* rewriting every URL builder and the
cookie model, because apps assume they own their subdomain origin. Much larger
blast radius. Rejected.

---

## Reaching a zone across subnets

mDNS is **link-local by design** — it rides multicast with no L3 routing, so
`.local` cannot cross into another subnet no matter what the responder does.
This is the case the multi-domain model exists for: `.local` is the
*same-segment* convenience name; anything off-segment needs a **unicast** name.

Scenario on record: Linux OpenHost boxes on one corporate subnet, browsers on a
different corporate subnet.

**Moving the browser's subnet** — possible but usually not endpoint-controlled.
On a managed network the switch port (access VLAN, 802.1X, DHCP snooping) decides
the subnet, so an endpoint can't unilaterally move. What works at the endpoint:

- **Second interface onto the box's segment** (e.g. USB-Ethernet dongle into a
  switch shared with the Linux box). The machine is then on both subnets; mDNS
  runs on the local-segment interface. This is the literal "be on both subnets."
- **VLAN sub-interface on a trunk port** — same idea, if the port is a trunk
  (corporate access ports usually aren't).

**Making the name reachable without moving** — the better options:

- **ZeroTier (virtual L2).** Emulated Ethernet segment that replicates
  multicast, so mDNS actually propagates across it — **`.local` keeps working
  across the physical subnet boundary** (watch its `multicastLimit`). The one
  overlay that preserves the `.local` model on a corporate network without
  touching corporate networking.
- **Tailscale (L3).** No broadcast/multicast, so mDNS does *not* traverse it —
  but MagicDNS gives a stable `*.ts.net` name reachable from any subnet. Reach
  the box by that name, not `.local`.
- **mDNS reflector/repeater** (Avahi `enable-reflector`, or a dedicated
  repeater) on a device multi-homed into both subnets. Technically works, but
  needs a box on both segments and admin cooperation; corporate networks usually
  block cross-subnet multicast on purpose. Fragile — not designed around.

**How the design absorbs this:** the same instance answers simultaneously on
`myapp.myhost.local` (http, mDNS) for same-segment/ZeroTier clients **and** a
routable name (`*.ts.net`, an internal corporate-DNS A record, or the public
`*.myhost.example.com` https) for cross-subnet clients — with no redirect
between them. The cross-subnet browser simply uses the routable domain. Turnkey
combos: **ZeroTier** if you want `.local` to keep working across subnets;
otherwise a **unicast domain served alongside `.local`**.

---

## Two public domains on one instance (status)

After Phases 0/1/3, an instance **routes and terminates TLS for two public
domains at once**. Remaining gaps for a clean end-to-end two-public-domain test:

- **Cert trust.** A second public TLS domain now acquires its **own real cert**
  when added via `POST /api/domains` (Phase 3b, per-domain ACME) — provided its DNS
  is delegated to this box's CoreDNS or handled by the cert_api broker. It serves via
  Caddy's internal CA (`tls internal`) while `acquiring`, then flips to the real cert
  (status `active`). Without DNS delegation, acquisition reports `error` and it stays
  on the self-signed cert.
- **Authoritative DNS.** ✅ CoreDNS is now authoritative for **every public (non-mDNS)
  domain**, not just the primary — one server block + zone file per domain, restarted on
  `POST/DELETE /api/domains`. Before this, a delegated secondary (NS → this box) got a
  bare `REFUSED` from CoreDNS because only the primary zone was configured (fixed
  2026-07-23). Each domain's ACME DNS-01 `_acme-challenge` TXT now lands in **its own**
  zone file, so DNS-01 works for secondaries too.
- **Phase 2 is done** ✅ → login, cookies, and dashboard links now stay on the
  arriving domain. So the *second* domain no longer bounces you to the primary;
  the only remaining gap for a fully first-class second **public** domain is the
  browser-trusted cert (Phase 3b above).
- **Config by hand.** Multi-domain configs aren't emitted by provisioning yet
  (Phase 5). Add them manually:
  ```toml
  [[openhost.domains]]
  name = "host.example.com"
  tls = true
  [[openhost.domains]]
  name = "host.example.org"
  tls = true
  ```
  Both must resolve to the box (real DNS or `/etc/hosts`), then restart the router.

## Implementation plan (phased)

Phases 0–2 are a pure refactor with **no behavior change for existing
single-domain installs** — they can land and be verified before any
`.local`-specific work.

### Phase 0 — Domain model in config (foundation, backward-compatible)

`compute_space/src/compute_space/config.py`
- Add a frozen attrs `Domain` class (`name: str`, `tls: bool`,
  `mdns: bool = False`), per `style_guide.md`.
- Add `Config.domains: tuple[Domain, ...] = ()`.
- `__attrs_post_init__` fallback: if `domains` is empty, synthesize
  `(Domain(zone_domain, tls_enabled),)` from the legacy fields, so old configs
  work unchanged.
- Keep `zone_domain`/`tls_enabled` as **the primary Domain** (`primary_domain`
  property = `domains[0]` / legacy field).
- Add the resolver — the heart of the refactor:
  `match_domain(host_no_port) -> Domain | None` (exact zone → router UI, or a
  single-label subdomain of a configured domain; longest-suffix wins so
  `myhost.local` and `myhost.example.com` don't collide).

`ansible/templates/config.toml.j2` + `routerd_cli/.../config_gen.py`: emit a
`[[openhost.domains]]` array; keep single-domain output as the default.

### Phase 1 — Per-request zone resolution + multi-domain routing

- `core/apps.py:1012` `get_app_from_hostname` → resolve against **any**
  configured Domain via `config.match_domain(...)`; return the matched Domain
  alongside the app.
- `web/middleware/subdomain_proxy.py`:
  - `_looks_like_app_subdomain` (`:84`) → "matches any configured Domain."
  - `_dispatch` (`:120`): compute the matched Domain once, stash it in the ASGI
    `scope` so downstream reads it without re-parsing.
  - `X-Forwarded-Proto` (`:166`) → derive from the **matched Domain's** `tls`,
    not the global flag. (This alone makes `.local`=http and public=https
    coexist at the proxy layer.)
- `web/app.py:157` `_reject_app_subdomain_requests` → check against any Domain.

### Phase 2 — Per-request scheme / URLs / cookies (kills "redirect to main")

Everywhere a link, redirect, or cookie is built inside a request, use the
**matched Domain from scope** instead of `config.zone_domain`/`tls_enabled`:

- `web/auth/auth.py:190` `build_login_url` → `{scheme}://{matched_zone}/login…`.
  An unauth'd hit on `myapp.myhost.local` logs in on `myhost.local`, not the
  public domain.
- `web/routes/pages/login.py:35` `_validated_next` + cookie set (`:61-75`) →
  accept next-URLs under any configured Domain; scope the session cookie to the
  matched parent domain.
- `web/auth/cookies.py:15,28` → `secure=` and `domain=` from the matched Domain.
- `web/app.py:72-99` template globals `app_url`/`zone_name`/`zone_domain` → make
  request-aware (inject `current_zone` from scope via a Litestar
  dependency/context-processor; `app_url(name)` builds on the current request's
  Domain). Fiddliest change — template globals are process-wide today.
- `web/setup_app.py:147`, `web/routes/pages/apps.py:166,172`,
  `web/routes/services_v2.py:170`, `web/routes/api/apps.py:258,606` → drop
  hardcoded `https://` / single-zone assumptions.

**Session note:** cookies can't span two registrable domains, so a user gets an
independent session on `.local` vs the public domain. Inherent and fine for a
single-owner zone.

### Phase 3 — Caddy per-host blocks (http `.local` + https external together)

`core/caddy.py:10` `generate_caddyfile` → emit one site block **per Domain**:

- For each `tls` Domain: `{name}, *.{name} { tls <cert> <key>; reverse_proxy
  localhost:8080 }` plus `http://{name}, http://*.{name} { redir
  https://{host}{uri} permanent }`.
- For each non-`tls` (mdns) Domain: `http://{name}, http://*.{name} {
  reverse_proxy localhost:8080 }` — **no redirect**. (Today's single `:80 redir`
  at `caddy.py:24` catches everything; this scopes the redirect to https
  domains only.)

Caddy `admin off`/restart-on-change is unaffected: wildcards cover all apps, so
adding/removing an *app* never rewrites Caddy — only adding/removing a *Domain*
does, reusing the existing `CaddyProcess.restart()` renewal path. `*.myhost.local`
in Caddy is just a match pattern; resolution is the responder's job.

TLS (`core/tls/*`, `web/start.py:67`): acquire certs only for `tls` Domains;
`.local` never touches ACME/CoreDNS. The public domain keeps its wildcard cert.

### Phase 3b — Domain management endpoint + per-domain ACME

**Goal:** make a domain a runtime-managed thing — add/remove one on a live instance
— and make **full browser-trusted ACME acquisition one code path used by both
initial setup and later addition**. This supersedes Phase 3's `tls internal`
fallback for any domain we can actually get a real cert for.

**The one acquisition routine.** Today cert acquisition (`core/tls/provision.py`
`provision_cert`) runs once at startup for the single primary domain. Refactor it
into an idempotent `ensure_cert_for(config, domain: Domain) -> CertPaths` that:
1. no-ops for non-TLS (`.local`) domains;
2. for a TLS domain, runs DNS-01 (via the instance's CoreDNS) or the `cert_api`
   broker — the existing two providers — for `[name, *.name]`;
3. writes the cert to a **per-domain path** (see below) and returns it.
Both the setup flow and the add-domain endpoint call exactly this. "Acquire a
cert for a domain" stops being startup-only.

**Per-domain cert storage.** Replace the single `tls_cert_path`/`tls_key_path`
with a per-domain layout, e.g. `openhost_data/certs/{name}.pem` + `{name}.key`
(keep the legacy pair as the primary's path for backward compat / migration).
`generate_caddyfile` then references each TLS domain's own cert files; `tls
internal` remains only the fallback for a TLS domain whose cert hasn't been
acquired yet (e.g. acquisition in progress or DNS not yet delegated). Renewal
(`core/tls/renewal.py`) iterates all TLS domains instead of one.

**Config persistence.** Adding a domain must persist to the config the router
reads on restart. Two options to decide:
- (a) The router owns `domains` in its own store (DB table `domains`), and the
  effective domain set is `config.domains` (file, provisioning-seeded) ∪ DB
  (runtime-added). Cleanest for a live API; the file stays declarative.
- (b) The router rewrites `config.toml`'s `[[openhost.domains]]`. Simpler model
  but the router now mutates a file provisioning also owns — contention risk.
Leaning (a): a `domains` table + a merged accessor, so runtime additions don't
fight ansible/`config_gen`.

**The endpoint** (owner-authed, on the router API, e.g. `web/routes/api/domains.py`):
- `GET /api/domains` — list configured domains + per-domain status
  (`cert: none|acquiring|active|error`, `discovery: public_dns|mdns`).
- `POST /api/domains` — body `{name, tls, mdns}`. Validates the name, adds it to
  the store, then **kicks off `ensure_cert_for` in the background** (acquisition is
  slow: DNS-01 propagation). Returns `202` with status `acquiring`; the row flips
  to `active` and Caddy is regenerated + `CaddyProcess.restart()`ed on success, or
  `error` with the reason on failure. An mDNS domain is active immediately (the
  wildcard responder already answers it; Phase 4).
- `DELETE /api/domains/{name}` — refuse to remove the primary; else drop it,
  regenerate Caddy, restart. (Optionally keep the cert files for re-add.)
- Dashboard: a "Domains" settings page over this API (add/remove, watch cert
  status) — the UI surface for what setup does on first boot.

**Initial setup uses the same path.** The `/setup` flow gains an optional
"additional domains" step; whatever the operator enters is added through the same
store + `ensure_cert_for` routine the endpoint uses. Result: setup and add-later
are the same code, so a second public domain gets a real cert either way.

**Hard constraint to surface in the UI.** DNS-01 for a *second* public domain
requires that domain's DNS be delegated to this box's CoreDNS (or handled by the
`cert_api` broker). If it isn't, acquisition can't succeed — the endpoint should
detect/report this (status `error`, actionable message) and the domain falls back
to `tls internal` until delegation is in place. mDNS `.local` domains have no such
requirement.

**Sequencing.** Depends on Phase 0 (Domain model — done) and Phase 3 (per-domain
Caddy blocks — done). Independent of Phase 4/5, but pairs naturally with Phase 5
(provisioning seeds the initial domain through the same routine).

### Phase 4 — Wildcard mDNS responder

New `core/mdns.py` (see decision above). Managed like `CoreDNSProcess`. LAN-IP
detection reuses `core/dns.py:77` `_coredns_bind_ip`. Started from
`web/start.py`, gated on an mdns Domain being configured. No app-lifecycle
hooks.

Platform: Linux-first (the provisioned box). On macOS a custom responder can't
bind 5353 (`mDNSResponder` owns it exclusively) — the wildcard path is
Linux-only; `openhost up` on a Mac laptop would fall back to a single `.local`
name or Bonjour registration (documented dev-laptop limitation, not the server
story).

### Phase 5 — Provisioning for local use

- `scripts/provision.sh` — add a `--local` mode (alongside `--local-http-only`)
  that provisions an instance whose primary Domain is `{name}.local` (mdns,
  http): starts the mDNS responder, skips CoreDNS/ACME, keeps Caddy for `:80` (or
  binds the router directly). This is the "provisioning script for local use."
- `ansible/` — extend `config.toml.j2` to emit the `.local` Domain; let full
  server mode **also** append a `.local` Domain so a public box is simultaneously
  reachable on the LAN (directly satisfies "always support multiple domains").
- `routerd_cli/.../up.py` + `config_gen.py` — first-class local mode for
  `openhost up` (writes a `.local` mdns Domain, starts the responder). Also
  resolves the `--dev` flag drift found in research (README documents `--dev`;
  `main.py` doesn't implement it).

### Phase 6 — Tests + docs

- Unit: `config.match_domain` (collision / longest-suffix / exact-zone);
  `generate_caddyfile` with mixed tls/non-tls Domains; `get_app_from_hostname`
  across two domains.
- Middleware: request on `.local` gets `X-Forwarded-Proto: http` + login bounce
  stays on `.local`; request on the public domain still gets https — asserting no
  cross-domain redirect.
- mDNS responder: parse/answer a wildcard query; ignore names outside the zone.
- Docs: update `docs/src/routing.md` (multi-domain + mDNS), note the
  OAuth/`OPENHOST_ZONE_DOMAIN` limitation.

---

## Risks / limitations (accepted)

- **Multi-label `.local` client resolution** varies by client resolver
  (macOS/Bonjour and Linux/systemd-resolved handle arbitrary `a.b.local`; older
  `nss-mdns` and bare Windows may not). Answering the query is easy; whether the
  client *asks* is up to it. **Left to the user; Mac not natively supported for
  this.**
- **Link-local only** — `.local` reaches only the same L2 segment (or a
  multicast-capable overlay like ZeroTier). Cross-subnet needs a unicast domain.
- **OAuth / external redirects** can't target `.local` — OAuth-dependent apps
  remain public-domain only. Apps should read `X-Forwarded-Host` for same-origin
  links; `OPENHOST_ZONE_DOMAIN` stays the canonical/primary. Documented, not
  fixed.
- **Sessions are per-domain** (cookie scoping). Expected.
- **macOS server** can't run the wildcard responder (5353 owned by
  `mDNSResponder`). Linux-first.
- Blast radius is wide but shallow: most of it is mechanically replacing two
  global reads with a per-request `Domain`.

---

## Single-domain inventory

Every point in the source (tests excluded) that records or assumes a single
domain, grouped by role. Legend: 🔴 must become **per-request matched Domain**
(routing/URL/cookie in the request path — where "redirect-to-main" lives) · 🟡
**global scheme** (`tls_enabled`) that must go per-request · 🟢 legitimately
**primary/canonical** (background/no-request context — keeps reading the primary
Domain) · ⚙️ **config / provisioning** entry point.

**A. Root definition (config)**
- ⚙️ `config.py:30` `zone_domain: str` · `config.py:118-119` `zone_domain_no_port`
- ⚙️ `config.py:37,235` `tls_enabled: bool` · `config.py:66,255` `my_openhost_redirect_domain`

**B. Routing / host-matching** 🔴
- `core/apps.py:1012-1033` `get_app_from_hostname` (`:1024,1026,1028-1029`)
- `web/middleware/subdomain_proxy.py:90` `_looks_like_app_subdomain`
- `web/app.py:167` `_reject_app_subdomain_requests`
- `web/routes/pages/login.py:24,35` `_validated_next` (callers `:44,59`)

**C. Per-request URL / redirect / link builders** 🔴 (proto sites also 🟡)
- `web/auth/auth.py:196` `build_login_url` (proto `:190`)
- `web/app.py:72-77,99` `app_url` template global (proto `:76`)
- `web/routes/pages/apps.py:172` OAuth action URL (proto `:166`)
- `web/routes/api/apps.py:258` add_app · `:606` reload_app return-to
- `web/routes/services_v2.py:170` approve URL (hardcoded `https`)
- `web/routes/docs.py:131-134` docs `zone_name`

**D. Cookies** 🔴 (secure also 🟡)
- `web/auth/cookies.py:15,28` `secure=tls_enabled`
- `web/routes/pages/login.py:62,75` · `web/setup_app.py:147` `cookie_domain=zone_domain_no_port`

**E. Proxy scheme header** 🟡 — `web/middleware/subdomain_proxy.py:166` `X-Forwarded-Proto`

**F. Caddy config generation** 🟡 — `core/caddy.py:10,12,83,87` `generate_caddyfile(tls_enabled,…)`

**G. Background / non-request** 🟢 (keep = primary Domain; `.local` never uses these)
- `web/start.py:126` start_coredns 🟠 (now iterates **all public domains**, not just the
  primary — CoreDNS is authoritative for every non-mDNS domain; see 2026-07-23 log) · `:134-146` TLS gating 🟡
- `core/dns.py:95,127,139,149,156,171` · `core/tls/provision.py:28,54` ·
  `core/tls/renewal.py:58,61` · `core/tls/util.py:47,58,181-182` ·
  `core/tls/acquire_cert_broker.py:72,81`
- `core/auth/identity.py:89,108` JWT issuer/sub · `core/containers.py:288` (comment)

**H. Env vars to apps** 🟢 — `core/data.py:83,169` `OPENHOST_ZONE_DOMAIN` ·
`core/data.py:82,171` `OPENHOST_MY_REDIRECT_DOMAIN` · `core/apps.py:392-393,646-647`

**I. Diagnostics / reporting** 🟢 — `core/diagnostics.py:219,241,773,806,637-638` ·
`web/routes/api/system.py:312-316,335` (download filename)

**J. CLI / provisioning / config generation** ⚙️ — `routerd_cli/.../config_gen.py:17,27,29` ·
`routerd_cli/.../up.py:142-159,167-176` · `ansible/templates/config.toml.j2:8,14`

The 🔴+🟡 sites (groups B–F) are the actual change surface for Phases 1–3; G–J
keep reading the **primary** Domain and only need the field re-sourced, not
per-request logic.

## Decision log

- **2026-07-21** — mDNS strategy: **custom wildcard responder** (`core/mdns.py`),
  not per-app Avahi publishing and not path routing. Keeps subdomain routing
  intact, no lifecycle hooks, no Avahi dependency. Mirrors the existing
  run-our-own-CoreDNS model.
- **2026-07-21** — Multi-label `.local` support is left to the user; macOS not
  natively supported for it.
- **2026-07-21** — Cross-subnet reach is out of scope for mDNS itself; handled by
  the multi-domain model (serve a unicast domain alongside `.local`) with
  ZeroTier / Tailscale / internal-DNS as the recommended options.
- **2026-07-21** — The per-request Domain is stashed in the ASGI scope under
  `openhost_zone` by `SubdomainProxyMiddleware` and read via
  `web/helpers/zone.py:zone_for_request`. Handlers that don't traverse the
  middleware (or hosts matching no domain) fall back to re-resolving from Host,
  then to the primary domain — so callers never special-case `None`.
- **2026-07-21** — Host matching became case-insensitive (Phase 1); the previous
  suffix check was case-sensitive. Accepted as a correctness improvement (Host
  headers are case-insensitive per spec).
- **2026-07-21** — Phase 2 scope split: navigation/login/cookies/app-links go
  per-request (arriving domain); OAuth `return_to` URLs and cross-app approval
  URLs stay on the **primary** domain (external callbacks need a stable domain, and
  the cross-app case has no browsing request in hand). Only the hardcoded `https`
  in the approval URL was fixed (→ primary's scheme).
- **2026-07-21** — Added Phase 3b to the plan (per user request): an owner-authed
  `/api/domains` endpoint and a single `ensure_cert_for(domain)` routine so full
  ACME acquisition is shared by initial setup and later domain addition; per-domain
  cert storage; a runtime `domains` store merged with the config-file set. This
  replaces the `tls internal` fallback for any domain that can get a real cert.
- **2026-07-23** — **CoreDNS made multi-zone.** The DNS layer (group G) was the last
  single-domain holdout: `start_coredns` took one `zone_domain` and the Corefile had one
  server block, so a *delegated* secondary domain got `REFUSED` (the box wasn't
  authoritative for it) even though routing/Caddy/certs were already per-domain. Now
  `start_coredns(zones, …)` emits one authoritative server block + zone file per **public**
  (non-mDNS) domain (`public_dns_zones(config)`), each ACME DNS-01 challenge writes to its
  own zone file (`Config.coredns_zonefile_path_for`), and `/api/domains` regenerates +
  restarts CoreDNS (new `CoreDnsProcess`/`reload_coredns_for_domains`, mirroring the active
  Caddy registry) — before kicking off acquisition, so the challenge zone is already served.
  mDNS `.local` domains stay out of CoreDNS entirely. Also switched the DNS Jinja env to
  `StrictUndefined` (a template typo silently rendered a blank `file` path).
- **2026-07-21** — Phase 3b persistence: runtime-added domains go in a router-owned
  `runtime_domains.json` (like `default_apps.json`), **not a DB table** (dropped the
  planned migration — a small single-owner list isn't relational data) and **not
  `config.toml`** (provisioning owns that file; the router must never rewrite it). The
  effective set = config-file domains ∪ runtime JSON, merged into the active config at
  startup and on every add/remove.

## Open questions

- _(none blocking — add here as they arise)_

## Progress

- [x] **Phase 0 — Domain model in config** _(landed 2026-07-21)_
  - Added frozen `Domain(name, tls, mdns)` value type + `name_no_port`/`scheme`.
  - `Config.domains: tuple[Domain, ...]` (empty by default); `all_domains`
    synthesizes a single primary from `zone_domain`+`tls_enabled` when unset, so
    single-domain configs are unchanged and serialize byte-identically (empty
    `domains` is dropped in `_to_toml_dict`).
  - `Config.primary_domain` and `Config.match_domain(host)` (longest-suffix,
    port-insensitive) resolver — not yet wired into routing (that's Phase 1).
  - Verified: populated `domains` round-trips through `typed_settings.load`;
    mypy strict + ruff clean; full lightweight suite (989 passed) green; new
    `tests/test_domains.py` (12 cases).
- [x] **Phase 1 — per-request zone resolution + routing** _(landed 2026-07-21)_
  - `core/apps.py` `get_app_from_hostname` now resolves via `config.match_domain`,
    so an app is reachable under **any** configured domain (e.g. both
    `<app>.host.example.com` and `<app>.myhost.local`). Signature unchanged
    (`App | None`) — its 3 callers are untouched.
  - `subdomain_proxy.py`: `_looks_like_app_subdomain` is multi-domain;
    `_dispatch` resolves the arriving Domain once, **stashes it in the scope**
    (`openhost_zone`), and sets `X-Forwarded-Proto` from that Domain's scheme
    (https for TLS, http for `.local`) instead of the global `tls_enabled`.
  - New leaf helper `web/helpers/zone.py`: `zone_for_request(connection) -> Domain`
    (reads the stash, else re-resolves from Host, else primary). Scope-key constant
    lives here to keep it cycle-free for Phase 2 consumers.
  - `web/app.py` `_reject_app_subdomain_requests` is multi-domain.
  - Minor intended behavior change: host matching is now **case-insensitive**
    (`match_domain` lowercases), where the old suffix check was case-sensitive.
  - Verified: mypy strict (161 files) + ruff clean; full suite green.
    - Unit: `tests/test_multidomain_routing.py` (12 cases) — host→app matching,
      `_looks_like_app_subdomain`, `_reject_app_subdomain_requests`, `zone_for_request`.
    - **Integration** (container-free, new): `tests/test_multidomain_proxy_integration.py`
      (4 cases) drives the real `SubdomainProxyMiddleware` over an in-process
      `httpx.ASGITransport` against a stub HTTP backend (stands in for a container
      on `127.0.0.1:<local_port>`). Proves the *same* seeded app is reachable
      under both `host.example.com` (proxied with `X-Forwarded-Proto: https`) and
      `myhost.local` (`X-Forwarded-Proto: http`) at once, unknown app subdomains
      404, and the router UI answers on both bare domains. This is the first test
      to exercise the middleware end to end.
  - No consumer reads the stashed zone yet, so single-domain behavior is unchanged.
- [x] **Phase 2 — per-request scheme / URLs / cookies** _(landed 2026-07-21)_
  - Introduced `zone_for_request(connection)` as the single source for the request's
    Domain, threaded through every request-path link/redirect/cookie site:
    - `web/auth/auth.py` `build_login_url(zone, …)` + `login_required_redirect` →
      login bounces to the **arriving** domain's `/login` over its own scheme (the
      change that removes "always redirect to main domain").
    - `web/routes/pages/login.py`: `_validated_next` accepts any configured domain;
      login/logout cookies scoped via `zone_for_request`.
    - `web/auth/cookies.py`: `build/clear_session_cookie(zone)` → `domain=zone.name_no_port`,
      `secure=zone.tls` (so a plain-http `.local` cookie isn't dropped).
    - `web/setup_app.py`: setup cookie scoped to the arriving domain.
    - `web/app.py`: `app_url` template global is now `@pass_context` request-aware —
      dashboard app links stay on the domain you're browsing.
    - `web/routes/pages/apps.py`: the "edit app" service action URL uses the arriving domain.
  - Intentionally left on the **primary/canonical** domain (documented): OAuth
    `return_to` URLs (`api/apps.py:258,606` — external callbacks need a stable domain),
    the cross-app approval URL (`services_v2.py` — server-side, no browsing request;
    hardcoded `https` fixed to the primary's scheme), and display-only branding.
  - Verified: mypy strict + ruff clean; full suite 1037 passed. New
    `tests/test_multidomain_urls.py` (9 unit cases) + 2 new integration cases proving an
    unauthenticated hit on a `.local` app subdomain 302s to `http://myhost.local/login`
    and a public one to `https://host.example.com/login` — no cross-domain bounce.
  - **Known limitation:** cookies can't span two registrable domains, so a login on
    `.local` and one on the public domain are independent sessions (expected).
- [x] **Phase 3 — Caddy per-host blocks** _(landed 2026-07-21, out of order — before Phase 2)_
  - `core/caddy.py` `generate_caddyfile` now takes the domain set and emits one
    site block per domain: a TLS domain serves `https://name, https://*.name`
    (+ a **scoped** `http://name…` redirect block) and a non-TLS `.local` domain
    serves `http://name, http://*.name` with **no redirect** — so http `.local`
    and https external coexist and `.local` is never bounced to https. The old
    global `:80 redir` catch-all is gone.
  - Cert source per TLS domain: the primary (`cert_domain`) uses the acquired
    file cert; any additional TLS domain uses Caddy's internal CA (`tls internal`).
    Global directive is `auto_https disable_redirects` when any TLS domain exists
    (so `tls internal` can still issue), else `off`.
  - `start_caddy` + `web/start.py` updated to pass `config.all_domains` and the
    primary cert; the "Caddy required for TLS" guard now triggers on *any* TLS
    domain (`any(d.tls for d in all_domains)`), not just the legacy `tls_enabled`.
  - Verified: mypy strict + ruff clean; full suite 1027 passed; new
    `tests/test_caddy_multidomain.py` (11 cases) incl. **real `caddy adapt`
    validation** of single-TLS, TLS+`.local`, two-public, and local-only configs;
    updated `test_integration.py::test_caddyfile_http_redirect` to the new format.
  - **Caveat:** additional public domains self-sign (`tls internal`) until
    per-domain ACME acquisition lands (Phase 3b) — fine for local/testing, not yet
    a browser-trusted cert for a *second* production public domain.
- [x] **Phase 3b — Domain management endpoint + per-domain ACME** _(landed 2026-07-21)_
  - **Persistence pivot (no DB migration):** runtime-added domains live in a
    router-owned `runtime_domains.json` under the data dir (like `default_apps.json`),
    not a DB table and not `config.toml`. `core/domain_store.py` owns it: `DomainRecord`
    (name/tls/mdns + cert status), atomic load/save, and the effective-set merge
    (`base` config domains + runtime records, deduped, primary first) swapped into the
    active config via `rebuild_active_domains`.
  - **Unified acquisition:** extracted `acquire_cert_for_domain(config, domain, cert, key)`
    from `provision_cert` (which now delegates for the primary — behavior unchanged), and
    added `core/tls/domain_certs.py:ensure_cert_for(config, domain)` — idempotent, no-op for
    mDNS, acquires to a **per-domain cert path** (`certs/<name>.pem`; primary keeps its legacy
    path). This is the one routine setup and the endpoint both call.
  - **Caddy:** `generate_caddyfile` now takes a `cert_for` resolver — a domain with an
    acquired cert file uses it, otherwise `tls internal`. Added a live-Caddy registry
    (`set_active_caddy`/`get_active_caddy`) + `reload_caddy_for_domains` so a request handler
    can regenerate + restart Caddy; `start.py` registers the process.
  - **Endpoint `web/routes/api/domains.py`** (owner-authed): `GET /api/domains` (list +
    per-domain cert status), `POST /api/domains` `{name,tls,mdns}` (validates, persists,
    swaps active config, reloads Caddy, and for a TLS domain kicks **background acquisition**:
    `acquiring` → `active`/`error`; mDNS is active immediately), `DELETE /api/domains/{name}`
    (refuses the primary). Registered in `app.py`; startup folds `runtime_domains.json` back
    into the active config so runtime domains survive restart.
  - Verified: mypy strict + ruff clean; full suite 1056 passed. New
    `tests/test_domain_store.py` (8: persistence/merge/ensure_cert_for) +
    `tests/test_domains_api.py` (11: CRUD, auth, validation, acquiring→active→error state
    machine with ACME stubbed). Real ACME still needs `--run-tls`/pebble + DNS delegation.
- [ ] Phase 4 — wildcard mDNS responder
- [ ] Phase 5 — provisioning
- [ ] Phase 6 — tests + docs
</content>
</invoke>
