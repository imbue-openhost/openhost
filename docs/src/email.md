# Email (Design)

This document describes how OpenHost instances send and receive email
for their zone, and why the design is shaped the way it is.

The platform-side pieces described here (the CoreDNS email records and the
provisioning that talks to the proxy) are implemented in this repo and gated
behind `email_enabled` in the instance config (off by default). The central
relay is a separate service,
[`openhost-email-proxy`](https://github.com/imbue-openhost/openhost-email-proxy),
deployed on fly.io. See [Production readiness](#production-readiness) for what
remains before turning this on for real tenants.

The guiding constraint is that **OpenHost instances are operated by
untrusted users**. A single instance must never be able to damage email
deliverability for any other instance, exhaust a shared resource, or
send mail claiming to be a domain it does not own. Every decision below
follows from that constraint.

## Why email needs platform support

Two facts make email different from a normal app:

1. **Cloud providers block port 25.** Most hosts (Hetzner, GCP, and
   others) block inbound *and* outbound TCP/25 to fight spam. An
   instance therefore cannot deliver mail directly to a recipient's MX,
   nor accept mail directly from a sender's MX. A relay is mandatory —
   this is not optional plumbing.

2. **Deliverability is a shared, reputation-based resource.** Whether
   mail lands in an inbox depends on the sending IP's reputation and on
   SPF/DKIM/DMARC alignment. If every instance sent from its own cloud
   IP, one abusive tenant would get that IP block-listed and, worse, a
   shared address space would let one tenant poison the reputation of
   all others.

Both facts point to the same answer: outbound mail flows through a
**central, OpenHost-operated SES relay** that owns the sending
reputation and enforces per-instance limits, rather than each instance
talking to the world directly.

Each instance runs a real mailbox server (**Stalwart**) and webmail
client (**Bulwark**) as default apps, giving a real inbox + outbox. The
mailbox server relays outbound mail through the central SES proxy as an
**SMTP smarthost**:

- the **email proxy** (`openhost-email-proxy`) runs an **SMTP submission
  relay** that holds the AWS SES credentials. Instances' Stalwart relays
  outbound to it over SMTP submission (AUTH), and it enforces From-domain
  scope + per-instance rate limits, then forwards to SES.
- **Per-instance SMTP auth is a stateless HMAC credential**: username =
  the instance's zone FQDN, password = `HMAC-SHA256(relay_secret, zone)`.
  vm-manager derives the same password at provision and hands it to the
  instance; the proxy re-derives + constant-time compares, learning the
  authorized zone from that one check. Rotating `relay_secret` rotates
  every instance credential.

This keeps the SES-credential-holding proxy off the public internet
(reached over Fly 6PN), gives instances a real inbox+outbox instead of a
send-only shim, and keeps the central multi-tenant safety guarantees
(From enforcement, rate limits, abuse controls) in one place.

> The SES **domain-identity creation** (to obtain DKIM tokens at
> provision) still uses a small HTTP call authenticated by the
> per-instance Keycloak client via the imbue-hosted-spaces frontend; only
> the high-volume *send* path moved to SMTP.

## The platform / app divide

The design splits along a single principle: anything **trust-critical or
multi-tenant-unsafe** is central or platform-level; anything
**user-facing and iterated often** is an app.

| Piece | Where | Why |
|-------|-------|-----|
| Email backend (SES relay, spam/abuse, SES identity + verification) | **Central, private** (Fly 6PN; holds AWS creds) | Shared reputation must be centrally governed; keep creds off the public internet |
| Auth boundary (verify instance token, proxy to backend) | **Central** (imbue-hosted-spaces frontend) | One authenticated public door, mirroring how it fronts vm-manager |
| Per-instance credential (Keycloak) | **Central-issued, instance-held** | Authenticates the instance and anchors From-enforcement |
| DNS records (DKIM/SPF/DMARC/MX) | **Platform** (CoreDNS) | Only the platform can write the authoritative zone |
| Provisioning (inject credential + mail config) | **Platform** | Part of instance finalize |
| Mailbox server (SMTP/JMAP + storage) | **App** (default) | User-facing, swappable, iterate-often |
| Webmail client | **App** (default) | Pure UX |

The frontend + private backend together are the trust boundary that
makes multi-tenant email safe. The mailbox and webmail pieces are
ordinary OpenHost apps shipped as defaults.

## Architecture overview

```
                       ┌──────────────────────────┐        ┌──────────────────────────────┐
   instance ──token──▶ │  imbue-hosted-spaces      │        │  email backend (PRIVATE, 6PN) │
   (Keycloak,          │  frontend (public door)   │ ─6PN─▶ │  · From = zone only           │ ──▶ AWS SES ─▶ MX
    calls /api/email)  │  · verifies instance token │        │  · per-instance rate caps     │     (outbound)
                       │  · derives zone            │        │  · spam / bounce / complaint  │
                       │  · sets X-OpenHost-Zone    │ ◀────  │  · SES identity + verify      │ ◀── SES inbound (S3+SNS)
                       └──────────────────────────┘        └──────────────────────────────┘
        │
        ▼
  CoreDNS on the instance writes DKIM / SPF / DMARC / MX
  (authoritative for the selfhost subzone; and for a BYO domain
   via optional NS delegation)

  On the instance: mailbox server (app) + webmail client (app)
```

## The email relay (frontend + private backend)

The relay runs on OpenHost-operated infrastructure (Fly), **separate
from any instance**, split into an authenticated frontend door and a
private backend.

**The private backend** holds the AWS SES credentials and does the SES
work. It has no public listener (reachable only over Fly 6PN), so it is
never exposed to the internet and cannot be called by an instance
directly. Its responsibilities:

- **Relay outbound mail to AWS SES**, which delivers to the recipient's MX.
- **Spam and abuse mitigation** for the whole fleet (see
  [Abuse controls](#abuse-controls)).
- **Create and verify SES domain identities.** When a zone is
  provisioned, the backend calls SES to create the domain identity, gets
  back the DKIM tokens, returns them so the instance can publish them in
  CoreDNS, and SES verifies once they resolve. SES will not send on
  behalf of an unverified domain, so this is mandatory — and automatic,
  not an operator step.
- **Handle inbound** via SES receiving (see [Receiving mail](#receiving-mail)).

The instance never holds AWS credentials. It only holds a Keycloak
credential that OpenHost can revoke at any time.

### Request authentication (frontend, Keycloak, cert-api pattern)

The **frontend** (imbue-hosted-spaces) is the authentication boundary —
the same role it already plays in front of vm-manager. An instance calls
the frontend's `/api/email/*` endpoints with a **Keycloak
client-credentials** token: each instance is provisioned with its own
confidential client in the `openhost-customers` realm (aud
`openhost-email`, a `subdomain` claim = the instance's zone), injected at
finalize time (see [Provisioning](#provisioning)), exactly mirroring the
cert-api pattern.

The frontend verifies the token locally against the realm's JWKS, reads
the `subdomain` claim, and proxies to the private backend over 6PN with a
trusted `X-OpenHost-Zone` header. The backend derives the sending domain
from that header — not from anything the instance asserts in the message
— which is what anchors From-domain enforcement. The header is
trustworthy only because the backend is unreachable except from the
frontend over the private network. Revoking a single instance is a matter
of disabling its Keycloak client; there is no shared secret to rotate.

### Abuse controls

Because the backend is the only way out and the frontend is the only way
in, that pair is where all multi-tenant safety lives:

- **From-domain enforcement.** The proxy rejects any message whose
  envelope-from or header-from is not within the instance's own
  (Keycloak-attested) zone. An instance can only ever send as
  `*@<its-own-zone>`. This cannot be enforced on the instance itself —
  only a party the tenant cannot bypass can enforce it.
- **Per-instance rate and volume caps**, so one tenant cannot consume
  the shared SES quota at the expense of others.
- **Reputation isolation.** Each instance (or cohort) is assigned a
  distinct SES **configuration set**, so bounce/complaint reputation is
  tracked per tenant and a bad actor's damage is contained; dedicated
  IPs can be assigned where needed.
- **Suppression and bounce/complaint handling**, maintained centrally so
  no instance can repeatedly hammer a bad address.
- **Automatic suspension** of an instance whose bounce/complaint rate
  crosses a threshold — per-instance, without affecting anyone else.

## DNS records (CoreDNS)

Each instance is authoritative for its own zone via CoreDNS (see
[Routing](./routing.md)). The email deliverability records are written
into that zone **automatically** — there is a single mechanism, no
per-provider connectors:

- **SPF** authorizing SES to send for the zone.
- **DKIM** public keys (the tokens the SES proxy obtained when creating
  the domain identity) so signed mail aligns.
- **DMARC** policy for the zone.
- **MX** pointing at the SES inbound endpoint.

Because these are persistent zone records (unlike the transient
ACME-challenge records), they are written as part of the zone's base
configuration so they survive router restarts, and the ACME-challenge
cleanup is scoped so that it never removes them.

### Bring-your-own domain (optional NS delegation)

A `<name>.selfhost.imbue.com` address works out of the box because the
parent zone already delegates each subzone to the instance's CoreDNS.

To use a **custom domain** (e.g. `me@mydomain.com`), the user delegates a
zone to the instance's CoreDNS with a single **NS record** at their
registrar — the same delegation model selfhost already uses. Once
delegated, CoreDNS is authoritative for that zone and OpenHost writes all
the email records automatically; no manual DKIM/verification steps at the
registrar.

Recommendation: delegate a **subdomain** (e.g. `mail.mydomain.com`)
rather than the apex, so OpenHost only becomes authoritative for the mail
subzone and the user keeps their existing website/DNS untouched.
Delegating the apex is possible for users who want OpenHost to serve all
of their DNS, but it is a bigger commitment and makes the instance's
CoreDNS load-bearing for the whole domain.

## Receiving mail

Inbound port 25 is blocked, so the instance cannot accept mail directly.
Instead:

1. **MX points at SES.** The instance's CoreDNS publishes an MX record
   directing mail for the zone to SES's inbound endpoint.
2. **SES receives and stores.** SES accepts the message, writes the raw
   RFC822 to an OpenHost-owned S3 bucket, and publishes an SNS
   notification.
3. **The proxy is notified and forwards to the instance.** SNS delivers a
   signed notification; the proxy **verifies the SNS signature**, fetches
   the raw message from S3, and hands it to the destination instance.
4. **The instance's mailbox server ingests it** via LMTP/SMTP, where the
   owner can read it.

Signature verification is mandatory: the inbound webhook is
internet-reachable, so an unverified payload would let anyone inject
mail. Only notifications with a valid AWS SNS signature from the expected
topic are accepted.

Inbound is also multi-tenant-sensitive — untrusted users receiving
unbounded mail is a storage/content-abuse vector — so the design applies
**per-instance inbound quotas** (rate and total mailbox storage) and
routes inbound mail only to the instance that owns the destination zone.

## The mailbox and webmail apps

The mailbox server (SMTP + JMAP with local storage) and the webmail
client ship as **default OpenHost apps**. Keeping them as apps means mail
data lives on the operator's own zone (not in a central store), and the
implementation can iterate without a platform release.

- The mailbox server relays outbound mail to the SES proxy (as a
  smarthost client) and ingests inbound mail delivered by the proxy.
- The mailbox server exposes its JMAP interface as a
  [cross-app service](./cross_app_services.md); the webmail app
  *consumes* that service.

## Access control — who can read the mail

Isolation has three layers; keep them distinct.

1. **Between instances (structural).** Each instance is a separate VM
   with its own mailbox server and its own storage, so one instance
   physically cannot read another's mail. The proxy additionally routes
   inbound mail only to the instance that owns the destination zone.
   This is the same isolation that already separates every OpenHost zone
   — nothing new is built for it.

2. **Within an instance, single owner (the common case).** Access to the
   webmail app and the mailbox is gated by **OpenHost owner
   authentication**: the router only stamps `X-OpenHost-Is-Owner: true`
   for the authenticated zone owner; everyone else is bounced to the zone
   login. A small proxy in front of the mailbox server validates the
   consuming app's permission grant, **strips any client-supplied
   credentials, and injects the owner's mailbox credentials** before
   forwarding. The webmail app never sees a mail password; the mail
   account's own password is internal and never user-facing. Access is
   governed entirely by OpenHost auth, not by knowledge of a mail
   password.

3. **Within an instance, multiple users (out of scope for now).** The
   current owner-auth model is binary — owner or not-owner — so every
   party who authenticates as the owner sees the same mailbox. Giving
   several distinct users on one zone their own private mailboxes would
   require mapping each authenticated OpenHost user to a specific mailbox
   in the JMAP proxy, which depends on OpenHost's federated-identity work
   and is not addressed here.

## Provisioning

Per-instance email configuration is injected at **finalize time**,
alongside the existing certificate-broker configuration:

- the instance's **Keycloak credential** (client id/secret) for the SES
  proxy,
- the proxy's base URL,
- and the zone's mail settings.

At provision, the SES proxy creates the SES domain identity and the DKIM
tokens are published into CoreDNS along with SPF/DMARC/MX; the proxy
polls SES until the domain verifies. For a `selfhost` subdomain this is
fully automatic. For a custom domain, the one manual step is the user's
NS delegation; everything after that is automatic.

## Trust and failure model

- **An instance can only send as its own zone**, enforced by the proxy
  from the Keycloak-attested identity.
- **An instance cannot damage others' deliverability** — reputation is
  isolated per SES configuration set; abuse triggers per-instance
  suspension only.
- **An instance holds no AWS credentials** — it authenticates to the
  proxy with a per-instance, individually-revocable Keycloak credential.
- **Proxy unavailability is fail-safe** — outbound mail queues on the
  instance's mailbox server and retries; inbound mail is retried by SNS.
- **Mail data stays on the instance** — the proxy relays and enforces
  policy but is not the mail store.

## Summary

| Concern | Where it lives | Why |
|---------|----------------|-----|
| Outbound relay + spam/abuse | Central SES proxy (fly.io) → AWS SES | Port 25 blocked; reputation is shared |
| Request auth | Keycloak (cert-api pattern) | Per-instance, revocable; anchors From-enforcement |
| From-domain safety | Central SES proxy | Only the proxy can enforce it against an untrusted tenant |
| SES identity + verification | Central SES proxy | SES won't send for an unverified domain |
| DKIM/SPF/DMARC/MX | CoreDNS (auto) | Instance owns its zone |
| Custom domains | Optional NS delegation → CoreDNS | Seamless BYO-domain via one record |
| Inbound receive | SES → S3 → SNS → proxy → instance | Port 25 blocked; signature-verified |
| Mailbox store + webmail | Default apps | Mail data stays on the operator's zone; iterate freely |
| Read access (single owner) | OpenHost owner auth + JMAP proxy | App never sees mail credentials |

## What is implemented today

- **Platform (this repo):** `email_*` config (opt-in, off by default),
  CoreDNS publishing of SPF/DKIM/DMARC/MX (`core/dns.py:apply_email_records`),
  a `clear_txt` fix that no longer wipes email TXT records on cert renewal, the
  proxy client + startup provisioning (`core/email/`), and finalize-time config
  injection (`ansible/templates/config.toml.j2`).
- **Proxy (`openhost-email-proxy`):** outbound send with Keycloak auth +
  From-domain enforcement + per-instance rate/volume caps, SES identity
  create/verify, and the signature-verified SNS inbound webhook with S3 fetch
  and per-zone routing to the destination instance.

Verified end-to-end on a fresh instance: it auto-published its DNS records, SES
auto-verified the domain from those records, and the instance sent DKIM-signed
mail through the proxy to a real external inbox. From-domain enforcement,
audience/issuer checks, rate limiting, and SNS signature rejection are covered
by tests.

## Production readiness

Before enabling email for real tenants, the following must be done. These are
deliberately **not** in scope of the initial implementation (they are
operational / infrastructure decisions or depend on other teams):

1. **AWS SES production access.** The account is in the SES sandbox, which only
   sends to verified recipients and caps volume. Request production access for
   the production AWS account/region before any real sending.
2. **A dedicated production AWS account.** Testing used a personal-scope account
   with inline IAM keys. Production should use a dedicated imbue AWS account, an
   IAM role/policy scoped to exactly the SES + S3 + SNS actions the proxy needs,
   and credentials delivered as fly secrets (never committed).
3. **Real per-instance Keycloak provisioning.** vm-manager must create the
   per-instance confidential client in `openhost-customers`, attach the
   `openhost-email` client scope (subdomain claim + `openhost-email` audience),
   set the service-account `subdomain` attribute, and inject the client
   credentials at finalize (the same step it will do for cert-api). Testing used
   a throwaway Keycloak and a manual provisioning script; production points
   `email_keycloak_issuer_url` at the real `keycloak.imbue.com` realm.
4. **Inbound infrastructure (SES receiving).** Create the S3 receiving bucket,
   the SNS topic subscribed to the proxy's `/v1/inbound`, and the SES receipt
   rule set that stores mail to S3 + notifies SNS. The proxy's inbound dispatch
   + signature verification are implemented and tested, but the AWS receipt
   pipeline itself must be stood up (and is only needed if inbound is desired;
   outbound works without it).
5. **Instance-side inbound endpoint + mailbox app.** The proxy forwards inbound
   mail to `https://<zone>/_email/inbound`; that endpoint (and the mailbox
   server + webmail default apps that consume it) is a separate piece of work.
6. **Proxy scaling / shared rate-limit store.** The proxy runs a single fly
   machine because the rate-limiter is in-process. Multi-machine HA needs a
   shared counter store (e.g. Redis/DynamoDB). Same constraint as cert-api.
7. **SES configuration sets per tenant.** Reputation isolation via per-tenant
   configuration sets (and dedicated IPs where warranted) plus bounce/complaint
   SNS handling and a suppression list should be provisioned before scale.
8. **DMARC policy + reporting.** The default published policy is
   `p=quarantine`; decide the production policy and a `rua` aggregate-report
   address per zone (config supports `email_dmarc_rua`).
9. **Canonical proxy URL + config defaults.** Point `email_proxy_base_url` and
   `email_inbound_mx_host` at the production proxy and SES region; consider a
   default once the production proxy has a stable DNS name.
