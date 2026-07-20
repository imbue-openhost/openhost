# Email (Design)

This document describes how OpenHost instances send and receive email
for their zone, and why the design is shaped the way it is. It is a
**design document** for a not-yet-fully-built capability; it describes
the intended architecture, not current behaviour.

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
**central, OpenHost-operated SES proxy** that owns the sending
reputation and enforces per-instance limits, rather than each instance
talking to the world directly.

## The platform / app divide

The design splits along a single principle: anything **trust-critical or
multi-tenant-unsafe** is central or platform-level; anything
**user-facing and iterated often** is an app.

| Piece | Where | Why |
|-------|-------|-----|
| SES proxy (relay, spam/abuse mitigation, SES identity + verification) | **Central** (imbue infra, e.g. fly.io) | Holds AWS creds; shared reputation must be centrally governed |
| Per-instance credential (Keycloak) | **Central-issued, instance-held** | Authenticates the instance and anchors From-domain enforcement |
| DNS records (DKIM/SPF/DMARC/MX) | **Platform** (CoreDNS) | Only the platform can write the authoritative zone |
| Provisioning (inject credential + mail config) | **Platform** | Part of instance finalize |
| Mailbox server (SMTP/JMAP + storage) | **App** (default) | User-facing, swappable, iterate-often |
| Webmail client | **App** (default) | Pure UX |

The SES proxy is the crux: it is the trust boundary that makes
multi-tenant email safe. The mailbox and webmail pieces are ordinary
OpenHost apps shipped as defaults.

## Architecture overview

```
                          ┌─────────────────────────────────────┐
                          │      OpenHost SES Proxy (central)     │
                          │  · Keycloak-verifies each request     │
   instance A ──submit──▶ │  · enforces From = own zone only      │ ──▶ AWS SES ─▶ recipient MX
   instance B ──submit──▶ │  · per-instance rate / volume caps    │      (outbound)
                          │  · spam / bounce / complaint handling │
   instance A ◀─webhook── │  · abuse detection + auto-suspend     │ ◀── SES inbound (S3 + SNS)
                          │  · creates + verifies SES identities  │
                          └─────────────────────────────────────┘
        │                                                          ▲
        ▼                                                          │
  CoreDNS on the instance writes DKIM / SPF / DMARC / MX ──────────┘
  (authoritative for the selfhost subzone; and for a BYO domain
   via optional NS delegation)

  On the instance: mailbox server (app) + webmail client (app)
```

## The SES proxy (central)

The proxy runs on OpenHost-operated infrastructure (e.g. fly.io),
**separate from any instance**. It is the direct analog of the existing
certificate broker (`cert-api`): a central service holding privileged
upstream credentials that instances talk to over an authenticated
channel, never touching those credentials themselves.

Its responsibilities:

- **Relay outbound mail to AWS SES.** Instances submit mail to the
  proxy; the proxy relays it to SES SMTP submission (port 587, which
  cloud hosts allow), which delivers to the recipient's MX.
- **Spam and abuse mitigation** for the whole fleet (see
  [Abuse controls](#abuse-controls)).
- **Create and verify SES domain identities.** When a zone is
  provisioned, the proxy calls SES to create the domain identity, gets
  back the DKIM tokens, hands them to the instance's DNS layer to
  publish, and polls SES until the domain is verified. SES will not send
  on behalf of an unverified domain, so this loop is mandatory and is
  the proxy's job — not the operator's.
- **Handle inbound** via SES receiving (see [Receiving mail](#receiving-mail)).

The instance never holds AWS credentials. It only holds a Keycloak
credential that OpenHost can revoke at any time.

### Request authentication (Keycloak, cert-api pattern)

Every request an instance makes to the proxy is authenticated with
**Keycloak client credentials**, exactly mirroring the certificate
broker: each instance is provisioned with its own confidential client in
a shared realm, and the client credentials are injected at finalize time
(see [Provisioning](#provisioning)).

The instance's Keycloak identity encodes which zone it is, so the proxy
learns the instance's true sending domain from the authenticated token
— not from anything the instance asserts in the message. This is what
anchors From-domain enforcement. Revoking a single instance is a matter
of disabling its client; there is no shared secret to rotate.

### Abuse controls

Because the proxy is the only way out, it is where all multi-tenant
safety lives:

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
