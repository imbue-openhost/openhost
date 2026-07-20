# Email (Design)

This document describes how OpenHost instances send and receive email
for their zone (`user@<zone_domain>`), and why the design is shaped the
way it is. It is a **design document** for a not-yet-built capability;
it describes the intended architecture, not current behaviour.

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
   nor accept mail directly from a sender's MX. Some relay is
   mandatory — this is not optional plumbing.

2. **Deliverability is a shared, reputation-based resource.** Whether
   mail lands in an inbox depends on the sending IP's reputation and on
   SPF/DKIM/DMARC alignment. If every instance sent from its own cloud
   IP, one abusive tenant would get that IP block-listed and, worse, a
   shared address space would let one tenant poison the reputation of
   all others.

Both facts point to the same answer: mail flows through a **central,
OpenHost-operated relay** that owns the sending reputation and enforces
per-instance limits, rather than each instance talking to the world
directly.

## Architecture overview

```
                          ┌─────────────────────────────────────┐
                          │        OpenHost Mail Broker          │
                          │  (central, OpenHost-operated)        │
   instance A ──submit──▶ │  · authenticates instance (Keycloak) │ ──▶ SES SMTP  ─▶ recipient MX
   instance B ──submit──▶ │  · enforces From = own zone only     │      (outbound)
                          │  · per-instance rate / volume caps   │
                          │  · bounce / complaint suppression    │
   instance A ◀─webhook── │  · abuse detection + auto-suspend    │ ◀── SES inbound (S3 + SNS)
                          └─────────────────────────────────────┘
```

- **Sending** (outbound): the instance hands each message to the broker;
  the broker relays it to the upstream email provider (AWS SES) and is
  the single point where policy is enforced.
- **Receiving** (inbound): the upstream provider accepts mail for the
  zone, stores the raw message, and notifies the broker, which delivers
  it into the instance's mailbox.
- The **mailbox itself** (the SMTP/JMAP server the owner reads with a
  webmail app) runs *on the instance*, so mail data stays on the
  operator's own zone.

The broker is the crux of the design: it is the trust boundary that
makes multi-tenant email safe.

## Sending mail

### Path

1. An app on the instance submits a message to a local submission
   endpoint (standard SMTP on `localhost`, or the mailbox server's
   outbound hook). The instance's mailbox server is configured as a
   **smarthost client**: it does not deliver directly, it relays every
   outbound message to the broker.
2. The instance authenticates to the broker with a **per-instance
   credential** and submits the message.
3. The broker validates and relays the message to SES SMTP submission
   (port 587, which cloud hosts allow), which delivers it to the
   recipient's MX.

The instance never holds AWS credentials. It only holds a broker
credential that OpenHost can revoke at any time.

### Instance authentication

The broker authenticates each instance with **Keycloak client
credentials**, mirroring the existing certificate-broker pattern: every
instance is provisioned with its own confidential client in a shared
realm, and the client credentials are injected at finalize time (see
[Provisioning](#provisioning)). Revoking a single instance is a matter
of disabling its client — no shared secret to rotate.

### Abuse controls (the reason for the broker)

Because the broker is the only way out, it is where all multi-tenant
safety lives:

- **From-domain enforcement.** The broker knows each instance's true
  zone domain (from the authenticated identity, not from anything the
  instance asserts). It **rejects any message whose envelope-from or
  header-from is not within that instance's own zone.** An instance can
  only ever send as `*@<its-own-zone>`. This is not enforceable on the
  instance itself — only a party the tenant cannot bypass can enforce
  it, which is exactly the broker.
- **Per-instance rate and volume caps.** Each instance gets its own
  send-rate and daily-volume budget. One tenant cannot consume the
  shared SES quota at the expense of others.
- **Reputation isolation.** The broker assigns each instance (or cohort
  of instances) to a distinct SES **configuration set**, so bounce and
  complaint reputation is tracked per tenant rather than pooled. A bad
  actor's reputation damage is contained to their own configuration
  set, and can be given dedicated IPs if needed.
- **Suppression and bounce/complaint handling.** The broker consumes
  SES bounce/complaint notifications, maintains a suppression list, and
  refuses to send to addresses that have hard-bounced or complained —
  centrally, so no instance can repeatedly hammer a bad address.
- **Automatic suspension.** An instance whose bounce or complaint rate
  crosses a threshold is automatically throttled or suspended, and the
  event is surfaced to OpenHost operators. Suspension is per-instance
  and does not affect anyone else.

### DNS for deliverability

Each instance is authoritative for its own zone (see
[Routing](./routing.md)), so the records that make outbound mail
deliverable are published **into the instance's own CoreDNS zone at
provision time**, with no manual operator step:

- **SPF** authorizing the broker/SES to send for the zone.
- **DKIM** public keys so signed mail aligns.
- **DMARC** policy for the zone.

Because these are persistent zone records (unlike the transient
ACME-challenge records), they are written as part of the zone's base
configuration so they survive router restarts, and the ACME-challenge
cleanup is scoped so that it never removes them.

## Receiving mail

Inbound port 25 is blocked, so the instance cannot accept mail directly.
Instead:

1. **MX points at the upstream provider.** The instance publishes an MX
   record (in its own CoreDNS zone) directing mail for the zone to SES's
   inbound endpoint.
2. **SES receives and stores.** SES accepts the message and writes the
   raw RFC822 to an OpenHost-owned S3 bucket, then publishes an SNS
   notification.
3. **The broker is notified and forwards to the instance.** SNS delivers
   a signed notification; the broker (or a per-instance inbound webhook
   fronted by the broker) **verifies the SNS signature**, fetches the
   raw message from S3, and hands it to the destination instance.
4. **The instance's mailbox server ingests it.** The message is
   delivered into the instance's local mailbox (via LMTP/SMTP into the
   mailbox server), where the owner can read it.

Signature verification is mandatory: the inbound webhook is
internet-reachable, so an unverified payload would let anyone inject
mail. Only notifications with a valid AWS SNS signature from the
expected topic are accepted.

### Inbound abuse considerations

Receiving is also multi-tenant-sensitive: untrusted users receiving
unbounded mail is a storage- and content-abuse vector. The design
applies **per-instance inbound quotas** (message rate and total
mailbox storage) enforced before delivery, and routes inbound mail
only to the instance that owns the destination zone.

## The mailbox and reading mail

The mailbox server (SMTP + JMAP) runs **on the instance** as an app so
that mail contents live on the operator's own zone rather than in a
central store. It stores mail in local per-app data so it survives
rebuilds.

Reading mail is done by a **webmail app** that talks to the mailbox
server. The mailbox server exposes its JMAP interface as a
[cross-app service](./cross_app_services.md); the webmail app *consumes*
that service. A small proxy in front of the mailbox server:

- validates the consuming app's permission grant,
- strips any client-supplied credentials, and
- injects the owner's mailbox credentials before forwarding.

This is the same trust model as other cross-app services: the consuming
app never sees mailbox credentials; it authenticates to the router with
its app token, and the provider proxy vouches for it. Access to the
mailbox is gated by **OpenHost owner authentication**, not by a mail
password.

## Provisioning

Per-instance email configuration is injected at **finalize time**,
alongside the existing certificate-broker configuration:

- the instance's **broker credential** (Keycloak client id/secret),
- the broker base URL,
- and the zone's mail settings (mailbox domain, DKIM key material).

At the same point, the SPF/DKIM/DMARC/MX records are written into the
instance's CoreDNS zone. No manual AWS or DNS steps are required of the
operator — the instance can send and receive as soon as it is claimed.

## Trust and failure model

- **An instance can only send as its own zone.** Enforced by the
  broker, which derives the zone from the authenticated identity.
- **An instance cannot damage others' deliverability.** Reputation is
  isolated per configuration set; abuse triggers per-instance
  suspension only.
- **An instance holds no upstream (AWS) credentials.** It authenticates
  to the broker with a per-instance, individually-revocable credential.
- **Broker unavailability is fail-safe.** If the broker is unreachable,
  outbound mail queues on the instance's mailbox server and retries;
  inbound mail is retried by SNS. No mail is silently dropped.
- **Mail data stays on the instance.** Message contents live in the
  operator's own zone; the broker relays and enforces policy but is not
  the mail store.

## Summary

| Concern            | Where it lives            | Why |
|--------------------|---------------------------|-----|
| Outbound relay     | Central broker → SES SMTP  | Port 25 blocked; reputation is shared |
| From-domain safety | Central broker             | Only the broker can enforce it against an untrusted tenant |
| Rate / abuse / suppression | Central broker      | Multi-tenant isolation |
| Inbound receive    | SES → S3 → SNS → broker → instance | Port 25 blocked; signature-verified |
| SPF/DKIM/DMARC/MX  | Instance CoreDNS zone (auto) | Instance owns its zone |
| Mailbox store      | Instance (local app data)  | Mail data stays on the operator's zone |
| Reading mail       | Webmail app via JMAP cross-app service | Consuming app never sees mail credentials |
