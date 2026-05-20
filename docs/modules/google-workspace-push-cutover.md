# Google Workspace Push Cutover

## Scope

This document is the hard-cutover plan that brings Ariel's Gmail and Calendar
push notification path from "watch registers + renews against a mocked Google
API; reconcile poll is the live mechanism" to "live push delivers every event
end-to-end, with a public HTTPS callback, a Pub/Sub StreamingPull subscriber,
exactly-once delivery, a dead-letter topic, and daily watch renewal."

It owns the cutover only. The standing description of provider ingestion lives
in [proactivity.md](proactivity.md), which Phase 5 updates. The plan inherits
[../ai-first.md](../ai-first.md), [../simplicity.md](../simplicity.md),
[../cleanliness.md](../cleanliness.md), [../boundaries.md](../boundaries.md),
[../concurrency.md](../concurrency.md), [../correctness.md](../correctness.md),
[../control-flow.md](../control-flow.md),
[../keys-and-identities.md](../keys-and-identities.md),
[../operation-types.md](../operation-types.md), and
[../mutation-ordering.md](../mutation-ordering.md), and follows the precedent
of [proactivity-cutover.md](proactivity-cutover.md): delete the half-finished
machinery, keep one trigger-agnostic agent loop and a thin rail.

This document supersedes the "push notifications, if wanted later, ship as one
whole cutover" line in
[../schema-consolidation-cutover.md](../schema-consolidation-cutover.md) and
the deferred Gmail-push half of
[../north-star-cutover.md](../north-star-cutover.md).

## Cutover Policy

- The cutover is hard. There is no dual ingress path, no feature flag, no
  silent fallback to the reconcile poll for Gmail's "live" semantics. The
  reconcile poll survives in its own role as the backstop reconciler — not as
  a synonym for push.
- `ruff`, `ruff format --check`, `mypy src tests`, and the full `pytest` suite
  are green at every PR.
- Every alembic migration runs up and down.
- The Calendar HMAC token contract is replaced outright: the global-token-only
  validation is deleted and the per-channel token becomes load-bearing. No v1
  surface is preserved.
- Legacy env vars are renamed and the old names are deleted from
  `config.py` and `.env.example`. No alias support.
- Every API change cites the rule it satisfies: `ai-first.md`, `simplicity.md`,
  `boundaries.md`, `concurrency.md`, `correctness.md`,
  `keys-and-identities.md`.

## Thesis

Live push is not a new capability of Ariel. It is the *only* delivery model
for "something changed in the user's Google Workspace and the agent should
know about it now, not at the next 60-minute poll." The agent loop already
handles a `provider_event_received` task identically regardless of source.
Push, in this sense, is plumbing — three layers of plumbing that today are
absent, half-present, or stubbed:

1. A public HTTPS ingress at a stable FQDN that Google can reach
   (`ariel.nielseriknandal.com`), terminated by Caddy and forwarded to the
   loopback-bound API.
2. A Pub/Sub StreamingPull subscriber that consumes the Gmail watch topic,
   inserts a `ProviderEventRecord`, enqueues a `provider_event_received` task,
   and acknowledges the message — running as its own systemd unit, mirroring
   the Discord bot's "long-lived listener as a sidecar" pattern.
3. A Calendar webhook handler that validates **per-channel** HMAC tokens
   against the existing `ProviderWatchChannelRecord` rows, not a single shared
   secret.

Each layer is one of the three legs of the same stool. None of them ship a new
agent surface. The wake side — the model deciding what an inbound event
means — already exists and is unchanged.

## What This Replaces

The May 2026 SME survey found the push path over-decorated and under-finished:

- `app.py` `POST /v1/providers/google/events` (`~3633-3795`) validates a single
  shared HMAC token against `app.state.google_provider_event_token`. The
  per-channel `ProviderWatchChannelRecord.channel_token` is generated, stored
  (`google_connector.py:~5080-5095`), but **never read for verification**.
  Single-token gating is multi-tenant-hostile and weaker than the storage
  already in place implies.
- Gmail watch registers a Pub/Sub `topicName` (`google_connector.py:~5042-5046`)
  but the codebase contains **no Pub/Sub subscriber** of any kind — no
  `google.cloud.pubsub_v1` import in `src/`, no `SubscriberClient` invocation,
  no pull loop, no push endpoint that decodes Pub/Sub envelopes. Gmail
  notifications are delivered to a topic that drains nowhere; the reconcile
  poll silently masks the loss.
- The Calendar webhook URL is configured as a single full URL
  (`ARIEL_GOOGLE_PROVIDER_EVENT_URL`). The OAuth redirect URI is hardcoded to
  `http://127.0.0.1:8000/...` (`config.py:58`). Neither composes with a public
  domain without a per-deploy hand-edit.
- The `ariel-api` systemd unit binds `127.0.0.1:8000` and ships without any
  reverse-proxy configuration in `deploy/`. The `docs/production-runbook.md`
  Google Workspace section names `ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN` and
  nothing else; GCP project setup, Pub/Sub topic creation, IAM grants, and
  DNS/TLS are not in the runbook.
- The watch-renewal scheduler runs every 6 hours with a 24-hour lead
  (`worker.py:~173,207`), which under Gmail's 7-day expiry effectively renews
  every ~6 days — within the cap but well outside Google's recommended daily
  cadence.
- Tests exercise the watch-registration API contract and the post-notification
  sync logic, but **no test sends a Pub/Sub-shaped delivery and asserts the
  end-to-end "live event" path**. The mock pattern is sound for the polled
  half and silent on the pushed half.

The cutover deletes the half-built shape (single shared token, unset
Pub/Sub-topic-with-no-consumer, hardcoded loopback redirect, undocumented
runbook) and replaces it with the three plumbing layers above.

## Goals

- A new Gmail message in a connected account causes a `provider_event_received`
  task to be enqueued within seconds, via Cloud Pub/Sub, end-to-end.
- A new Calendar event in a connected account causes the same, via Google's
  HTTPS push channel, end-to-end.
- Both watches renew on Google's recommended cadence (Gmail: daily) without
  operator intervention, and renewal failures surface as a wake to the user.
- The webhook URL is constructed from a single base-URL setting that also
  drives the OAuth redirect URI; one DNS record and one Caddy block carry the
  whole Google integration.
- The Pub/Sub subscriber runs as its own systemd unit and is supervised
  symmetrically with `ariel-api`, `ariel-worker`, and `ariel-discord`.
- The subscriber's health is visible from `/v1/health` in DB-as-state form,
  matching the codebase's existing observability pattern.
- Inbound auth is layered: TLS at the proxy, a global HMAC token gate at the
  app, per-channel HMAC validation against the stored `channel_token`. A
  malformed or stale push is rejected at the boundary; the worker sees only
  validated, deduplicated, normalized events.
- The cutover is operator-ready: a `gcloud` provisioning script idempotently
  creates the topic, subscription, dead-letter topic, dead-letter
  subscription, and IAM bindings; a Caddy install script idempotently
  provisions TLS and the narrow reverse-proxy rule.
- One PR per phase. Each PR is independently shippable and `make verify`-green.

## Non-Goals

- No multi-tenant Pub/Sub topology. The deployment is single-tenant (one Ariel,
  one user). One topic, one subscription, one DLQ topic, one DLQ subscription.
  Per-user subscriptions are not introduced.
- No HTTP-push Pub/Sub mode. Pull StreamingPull only. The webhook handler
  remains Calendar-only on the HTTPS path; Pub/Sub never POSTs to it.
- No background scheduler embedded in the subscriber. The subscriber inserts
  one `ProviderEventRecord` and enqueues one `provider_event_received` task.
  All downstream work runs in `ariel-worker`.
- No live UI affordance ("inbox: live push: on"). v2 is the data path. The
  Discord-side health surface is a follow-up.
- No alternate authentication mode for the subscriber's own GCP identity.
  Service-account JSON file at a chmod-600 path is the only supported model
  for v2; workload identity federation is a future migration.
- No re-armed watch-creation on Gmail-side `404` or "watch already exists"
  errors during the same renewal cycle — the existing renewal scheduler owns
  that. The subscriber does not register watches.
- No `notifications` table, no `subscription_events` table, no
  push-status-per-user table. The existing `ProviderEventRecord` is the
  event log; `ProviderWatchChannelRecord` is the channel log;
  `subscriber_heartbeat` is the new — and only — operational-state table.
- No alembic destructive op on a table that has never carried production data.
  All schema changes are additive (one new table, no column drops).
- No support for the legacy `ARIEL_GOOGLE_PROVIDER_EVENT_URL` setting after
  P2. It is deleted from `config.py`, `.env.example`, and the runbook.
- No support for the legacy "shared global token alone is sufficient"
  validation path after P2. The global token is reduced to a coarse first
  gate; per-channel HMAC is mandatory for acceptance.

## Target Architecture

### One mental model, two ingress paths

Push notifications arrive on one of two paths because Google's APIs ship two
different push models:

- **Calendar** uses HTTPS webhook channels: Google POSTs to a URL given at
  `events.watch` time. The POST carries `X-Goog-*` headers and an empty (or
  small) body. Auth is a pre-shared token sent in `X-Goog-Channel-Token`.
- **Gmail** uses Cloud Pub/Sub: `users.watch` names a topic, Google publishes a
  notification message into the topic, the subscriber pulls and acks. Auth on
  the subscribe side is the Ariel service account's IAM Subscriber role on
  the subscription; auth on Google's publish side is the
  `gmail-api-push@system.gserviceaccount.com` service agent's IAM Publisher
  role on the topic.

The two paths converge at a single point: the insertion of a
`ProviderEventRecord` row, atomic with the enqueue of one
`provider_event_received` task. Beyond that point the system has no notion of
"how this event arrived" — the same worker handler runs whether the trigger
was a Calendar HTTPS POST or a Pub/Sub StreamingPull callback. This convergence
is the entire architectural payoff.

```
Calendar event change ──► Google ──► HTTPS POST ──► Caddy (TLS, narrow route)
                                                       │
                                                       ▼
                                                   ariel-api
                                                   /v1/providers/google/events
                                                   • global-token gate
                                                   • per-channel HMAC
                                                   • dedup by channel_id+message_number
                                                   │
                                                       ▼
                                            INSERT ProviderEventRecord
                                            ENQUEUE provider_event_received   ─┐
                                                                                │
Gmail event change ──► Google ──► Pub/Sub topic                                 │
                                       │                                        │
                                       ▼                                        │
                                  StreamingPull                                 │
                                       │                                        │
                                       ▼                                        │
                                  ariel-pubsub (sidecar)                        │
                                  • exactly-once subscription                   │
                                  • SDK lease management                        │
                                  • parse {emailAddress, historyId}             │
                                  • dedup by Pub/Sub messageId                  │
                                       │                                        │
                                       ▼                                        │
                                  INSERT ProviderEventRecord                    │
                                  ENQUEUE provider_event_received  ──────► ariel-worker
                                  ack_with_response()                       _wake
                                                                            (the agent loop)
```

The diagram is the architecture. Everything else in this document is detail
that lets the diagram be true.

### The Calendar HTTPS path

`POST /v1/providers/google/events?resource_type=calendar&resource_id={account}`

The route is preserved by URL but its auth and validation are rewritten:

- **Layer 1 — TLS.** Caddy terminates TLS for `ariel.nielseriknandal.com` and
  forwards only the request path `/v1/providers/google/events` (and the OAuth
  callback path `/v1/connectors/google/callback`) to `127.0.0.1:8000`. No
  other path on the public surface returns 200. The Server header is stripped;
  HSTS, X-Content-Type-Options, and Referrer-Policy are set.
- **Layer 2 — Global token gate.** The handler validates that
  `X-Goog-Channel-Token` matches the deployment's
  `ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN` using `hmac.compare_digest`. This is a
  coarse first gate that survives the cutover because it is cheap, simple,
  and the operator's "kill switch" if the per-channel token system ever needs
  to be invalidated wholesale (rotate the global secret, every channel
  rejects until reconnect).
- **Layer 3 — Per-channel HMAC.** After the global gate, the handler looks up
  the channel by `X-Goog-Channel-ID` in `ProviderWatchChannelRecord` and
  validates `X-Goog-Channel-Token` against the *per-channel*
  `channel_token` stored at watch-register time, again with
  `hmac.compare_digest`. A channel id with no row, or a row whose
  `channel_token` does not match, returns 401 — *before* any DB write.
  Per-channel HMAC is the boundary `boundaries.md` calls "parse, validate,
  narrow."
- **Idempotency.** The dedup key derives from
  `sha256("google:{resource_type}:{resource_id}:{channel_id}:{message_number}")`
  as today. A duplicate POST returns `202 {"duplicate": true}` without
  enqueueing a second task; a same-dedup-key POST with a different payload
  digest returns `409 E_PROVIDER_EVENT_CONFLICT`. Both are preserved.
- **Handoff.** On first occurrence, insert a `ProviderEventRecord` with
  `status='accepted'` and enqueue a `task_type='provider_event_received'`
  background task carrying `{"provider_event_id": <id>}`. Return `202
  Accepted`.

The handler never touches Google's APIs. It does not call `events.list`. It
does not load tokens. It is a pure ingress rail: parse → authenticate (twice)
→ dedupe → enqueue → ack. `ai-first.md`'s rails-vs-judgment split.

### The Gmail Pub/Sub path

A new module, `src/ariel/pubsub_subscriber.py`, runs as its own systemd unit
`ariel-pubsub.service`. It is a long-lived process whose lifecycle is owned
by systemd, not by the API or the worker. This matches the existing precedent
of `ariel-discord.service` (which is also a long-lived listener for a
push-callback API), and obeys the `proactivity.md` rule that
`background_tasks` is owned by a single serialized worker.

At startup:

1. Resolve `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION` (a full resource path:
   `projects/{project}/subscriptions/{name}`) and
   `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH` (a filesystem path to the
   service-account JSON, chmod 600, owned by user `niels`).
2. Set `GOOGLE_APPLICATION_CREDENTIALS` from the path so the Google SDK picks
   it up via ADC.
3. Build a `SubscriberClient` and call
   `get_subscription(subscription=...)` — a one-shot existence check. A
   missing subscription fails the process with a clear log line; the
   provisioning script is the only path that creates it.
4. Configure flow control:
   `FlowControl(max_messages=20, max_lease_duration=600, max_bytes=10*1024*1024)`.
5. Open a streaming pull via
   `subscriber.subscribe(subscription, callback=_on_message,
   flow_control=..., await_callbacks_on_shutdown=True)`. The returned
   `StreamingPullFuture` is held; the process awaits it until SIGTERM.

On each message, `_on_message(message)`:

1. Decode `message.data` as UTF-8 JSON. The payload schema is
   `{"emailAddress": str, "historyId": int}`; any other shape `message.nack()`
   and is logged. The DLQ's `max_delivery_attempts=10` will catch persistently
   malformed messages.
2. Resolve `emailAddress` to a `GoogleConnectorRecord` by `account_subject` /
   `account_email`. Unknown account: `message.ack_with_response()` and exit
   without DB writes — a non-Ariel-owned mailbox shouldn't pin the
   subscription.
3. Compute the dedup key
   `sha256("google:gmail:{account_subject}:pubsub:{message.message_id}")`.
4. Insert a `ProviderEventRecord` with `provider="google"`,
   `resource_type="gmail"`, `resource_id=<account_subject>`,
   `event_type="pubsub_notification"`, `dedupe_key=<key>`,
   `body_digest=sha256(message.data)`,
   `payload={"emailAddress": ..., "historyId": ..., "pubsub_message_id": ...,
   "publish_time": ...}`, `status='accepted'`. The insert is wrapped in
   `SELECT ... FOR UPDATE` on the dedup-key row, matching the Calendar path.
5. Enqueue a `provider_event_received` task pointing at the new
   `ProviderEventRecord.id`. Same handler the Calendar path uses; no
   downstream branch.
6. Call `message.ack_with_response()` and await the future. On
   `AcknowledgeStatus.SUCCESS`, the message is durably acked. On any other
   status, log and rely on Pub/Sub to redeliver (the message will be re-leased
   when the deadline expires; the dedup key will short-circuit the re-insert).
7. Update `subscriber_heartbeat.last_message_at = now()` on success.

The subscriber writes a heartbeat row every 30 seconds regardless of message
flow (`last_seen_at`) and increments `in_flight_count` / `errors_in_window`
in the same row. The row is read by `/v1/health` to project subscriber
liveness without introducing a metrics exporter.

The startup verifies the subscription exists; **it does not create it**. The
provisioning script (operator-owned, `gcloud`-based) is the sole path that
creates the topic, subscription, DLQ topic, DLQ subscription, and IAM
bindings. The runtime SA has `roles/pubsub.subscriber` + `roles/pubsub.viewer`
on the source subscription (subscriber covers `consume`; viewer covers the
`get` call the sidecar issues at startup) and `roles/pubsub.subscriber` on the
DLQ subscription, nothing more. Least privilege for the runtime;
provisioning runs out-of-band as the operator's own credentials.

### Auth model — three layers

Three distinct identity boundaries are at play. Untangling them is the
keys-and-identities.md compliance the cutover owes:

| Layer | What it authenticates | How |
|---|---|---|
| **User OAuth** (per connected Google account) | The Google user authorizes Ariel to read their Gmail and Calendar | OAuth Authorization Code flow, refresh token encrypted via `ConnectorTokenCipher` and stored on `GoogleConnectorRecord` |
| **Calendar push token** (per channel) | Google's outbound HTTPS push proves the channel is one Ariel registered | At `events.watch` time, Ariel generates `secrets.token_urlsafe(24)` per channel and stores it on `ProviderWatchChannelRecord.channel_token`. Inbound POSTs send it in `X-Goog-Channel-Token`. |
| **Ariel runtime SA** (one per deployment) | Ariel-as-a-service authenticates to its own GCP project to pull from the Pub/Sub subscription | Service-account JSON keyfile at `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH`, chmod 600. The Pub/Sub publish side (Google's `gmail-api-push@system.gserviceaccount.com`) is a separate Google-managed identity granted Publisher on the topic by the provisioning script. |

User OAuth is preserved unchanged. Calendar push tokens go from "stored but
unused" to "stored and required." Runtime SA is new for v2; it is the first
"act as the app" credential in the codebase and it composes by living on the
filesystem rather than in the encrypted-token table — the
keys-and-identities.md update in P5 records this rule.

### Dedup model — two layers

Push is at-least-once at both Google-Calendar's and Pub/Sub's delivery layer.
Dedup happens twice, at two different scopes:

- **Delivery-level dedup** (per-event-row): the existing
  `ProviderEventRecord.dedupe_key` unique constraint catches identical
  re-deliveries. For Calendar:
  `sha256("google:calendar:{resource_id}:{channel_id}:{message_number}")`.
  For Gmail: `sha256("google:gmail:{account_subject}:pubsub:{message_id})`.
  This stops duplicate notifications from creating duplicate tasks.
- **Application-level idempotency** (per Gmail history watermark): the
  existing `SyncCursorRecord.cursor_value` already tracks the
  last-successfully-processed Gmail historyId. The worker's
  `process_provider_sync_due` uses it as `startHistoryId`. The historyId in
  the Pub/Sub payload is informational only; it is **never** used as a
  `startHistoryId`. This was already the design (`sync_runtime.py:~300-324`)
  and is preserved verbatim — the cutover does not duplicate work the worker
  is already doing. We document the rule so future contributors do not
  "optimize" by piping the payload historyId into the call.

There is no DLQ for Calendar HTTPS; Google retries with backoff and abandons
after ~7 days, which is acceptable for an idempotent dedup-keyed insert.
There is a DLQ for Gmail Pub/Sub (`ariel-gmail-watch-dlq` topic with
`ariel-gmail-watch-dlq-sub` subscription) so persistently malformed messages
go somewhere the operator can see, not nowhere.

### Worker integration

The worker is untouched by this cutover. `process_provider_event_received`
already exists; it reads the `ProviderEventRecord` and enqueues a
`provider_sync_due` task for `(provider, resource_type, resource_id)`. That
task calls the existing `email_list_history` / `calendar_list_event_deltas`
delta-sync, which on success advances `SyncCursorRecord.cursor_value` and
enqueues an `agent_wake` if there is new data. Every step downstream of
"insert ProviderEventRecord + enqueue provider_event_received" is reused
verbatim. The agent loop's wake path is also unchanged.

The only worker change is the daily-renewal cadence in P4 — a single constant
edit. There are no new task types and no new dispatch arms.

### Reverse proxy boundary

Caddy is the single public ingress. The Caddyfile ships in
`deploy/caddy/Caddyfile`. The block forwards exactly two paths to the
loopback API and returns 404 for everything else, including bare-IP requests
and unknown subdomains:

```caddyfile
{
    email niels@trysolid.com
    servers {
        trusted_proxies static private_ranges
    }
}

ariel.nielseriknandal.com {
    log {
        output file /var/log/caddy/ariel-webhook.log {
            roll_size 50mb
            roll_keep 10
            roll_keep_for 720h
        }
        format json
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        -Server
    }

    handle /v1/providers/google/events {
        reverse_proxy 127.0.0.1:8000
    }

    handle /v1/connectors/google/callback {
        reverse_proxy 127.0.0.1:8000
    }

    handle {
        respond 404
    }
}
```

Auto-HTTPS via Let's Encrypt. No IP allowlist — Google publishes no stable
ranges for Calendar webhook source IPs and explicitly directs implementers
to validate `X-Goog-Channel-Token` instead.

### Process topology

After the cutover, the box runs four user-mode systemd units, all as user
`niels`, all under `Restart=always`:

| Unit | Role | Long-lived listener? |
|---|---|---|
| `ariel-api.service` | FastAPI on `127.0.0.1:8000`; webhook receiver, OAuth callback, agent ingress, `/v1/health` | yes (HTTP) |
| `ariel-worker.service` | The serialized `background_tasks` worker; watch renewal; sync; wakes | no — polling loop |
| `ariel-discord.service` | Discord bot; long-lived gateway socket | yes (Discord gateway) |
| `ariel-pubsub.service` | **NEW** — Pub/Sub StreamingPull subscriber for Gmail | yes (Pub/Sub gRPC stream) |

Plus `caddy.service` (provided by the Caddy apt package) as the public TLS
proxy.

Each unit has one job. Memory cost of `ariel-pubsub` on the 2 GB Hetzner VPS
is ~30-50 MB resident (Python + gRPC + a small in-flight set bounded by
`max_messages=20`); acceptable.

## Capability & Contract Surface

### HTTP — `POST /v1/providers/google/events`

| Field | Spec |
|---|---|
| Method | POST |
| Path | `/v1/providers/google/events` |
| Query | `resource_type` ∈ {`calendar`} (Gmail no longer uses this path), `resource_id` (defaults to `primary`) |
| Headers (required) | `X-Goog-Channel-Token`, `X-Goog-Channel-ID`, `X-Goog-Message-Number`, `X-Goog-Resource-State` |
| Headers (optional) | `X-Goog-Resource-ID`, `X-Goog-Changed`, `X-Goog-Channel-Expiration` |
| Body | JSON or empty |
| Auth | Global HMAC (`ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN`) **AND** per-channel HMAC against `ProviderWatchChannelRecord.channel_token` looked up by `X-Goog-Channel-ID` |
| Idempotency | Dedup on `sha256("google:calendar:{resource_id}:{channel_id}:{message_number}")`; duplicate returns `202 {"duplicate": true}`; conflicting payload returns `409 E_PROVIDER_EVENT_CONFLICT` |
| Success | `202 Accepted` with `{"duplicate": bool, "provider_event_id": str}` |
| Errors | `401` (either HMAC fails / channel unknown), `400` (missing required headers), `409` (conflict) |
| Side effects | One `ProviderEventRecord` row, one enqueued `provider_event_received` task |

`resource_type=gmail` and `resource_type=drive` on this route are **rejected
with 400** after P2. Gmail never reaches the HTTP path; Drive is out of scope.

### Pub/Sub subscriber callback

| Aspect | Spec |
|---|---|
| Subscription | `projects/{project}/subscriptions/ariel-gmail-watch-sub` (full path in `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION`) |
| Delivery mode | StreamingPull |
| Subscription type | Exactly-once delivery enabled (`enable_exactly_once_delivery=true` at create time) |
| Ack deadline | 60s on the subscription; SDK lease management may extend up to `max_lease_duration=600s` |
| Flow control | `max_messages=20`, `max_bytes=10*1024*1024`, `max_lease_duration=600` |
| DLQ | `projects/{project}/topics/ariel-gmail-watch-dlq` with `max_delivery_attempts=10` |
| Ack method | `message.ack_with_response()` with `AckResponse` future inspection |
| Payload schema | `{"emailAddress": str, "historyId": int}` (decoded from base64url'd `message.data`) |
| Dedup key | `sha256("google:gmail:{account_subject}:pubsub:{message.message_id}")` |
| On unknown account | Ack and drop; do not pin the subscription |
| On malformed payload | Nack; let DLQ catch it after 10 attempts |
| On `AckResponse != SUCCESS` | Log; rely on Pub/Sub redelivery + dedup short-circuit |

### `GET /v1/health` (extended)

| Field | Spec |
|---|---|
| Existing response | `{"ok": true}` |
| Extended response | `{"ok": bool, "subscribers": {"gmail_pubsub": {"last_seen_at": str, "last_message_at": str \| null, "in_flight_count": int, "errors_in_window": int}}}` when push is configured |
| Health rule | `ok=true` iff `subscriber_heartbeat.last_seen_at` is within the last `2 * heartbeat_interval` (default 60s window for a 30s heartbeat). Stale → `503 ok=false`. |

The endpoint stays unauthenticated (its current state) and stays narrow.
Caddy does NOT forward `/v1/health` publicly. Operators run
`curl http://127.0.0.1:8000/v1/health` on the box.

### Capability registry — no changes

This cutover ships no new sandboxed capability. The agent's syscall surface
is unchanged; the wake side is unchanged; `cap.proactive.schedule` is
untouched. Push is plumbing.

## Data Model End State

One new table. No drops, no destructive ops. Existing tables are reused
verbatim — the schema already supports everything except subscriber heartbeats.

### New: `subscriber_heartbeat`

A singleton-per-subscriber operational state row. The subscriber sidecar
updates it every 30s; `/v1/health` reads it. No history; the row is mutated
in place.

| Column | Type | Notes |
|---|---|---|
| `subscriber_name` | `varchar(64) PRIMARY KEY` | e.g., `"gmail_pubsub"` |
| `last_seen_at` | `timestamptz NOT NULL` | Updated every 30s regardless of message flow |
| `last_message_at` | `timestamptz` | Updated on successful message ack |
| `in_flight_count` | `integer NOT NULL DEFAULT 0` | Subscriber's view of leased messages |
| `errors_in_window` | `integer NOT NULL DEFAULT 0` | Rolling 5-minute error count, reset on heartbeat tick |
| `last_error_code` | `varchar(64)` | Free-form last-error tag |
| `last_error_at` | `timestamptz` | When `last_error_code` was set |
| `updated_at` | `timestamptz NOT NULL` | Standard updated_at |

Migration: `alembic/versions/<timestamp>_subscriber_heartbeat.py`. Up creates
the table; down drops it. No data backfill (the subscriber writes the row on
first heartbeat tick).

### Unchanged: `ProviderWatchChannelRecord`

Stored at `persistence.py:~958-1003`. Schema already carries the per-channel
`channel_token` field. P2 starts *reading* it; the schema does not move.

### Unchanged: `ProviderEventRecord`

Stored at `persistence.py`. The `dedupe_key` text column accepts any sha256
hex string; the Gmail path uses
`sha256("google:gmail:{account_subject}:pubsub:{message_id}")`. No new
columns.

### Unchanged: `SyncCursorRecord`

For Gmail, `cursor_value` is the last-successfully-processed `historyId`. The
worker reads it as `startHistoryId`. The Pub/Sub subscriber never touches it.

### Unchanged: `GoogleConnectorRecord`

The encrypted-OAuth-tokens-per-user record. The Pub/Sub runtime SA does *not*
live here. SA JSON lives on the filesystem.

## Settings End State

### Added (P1)

```python
public_webhook_base_url: str | None = None
google_pubsub_subscription: str | None = None
google_application_credentials_path: str | None = None
subscriber_heartbeat_interval_seconds: float = 30.0
subscriber_heartbeat_staleness_factor: float = 2.0
```

Validators:

- `public_webhook_base_url`: when set, must be an `https://` URL with non-empty
  netloc and no path/query/fragment; trailing slash stripped. Required in
  `deployment_mode == "production"`.
- `google_pubsub_subscription`: when set, must match
  `projects/[a-zA-Z0-9-]+/subscriptions/[a-zA-Z0-9_-]+`. Validation only —
  no resource existence check at boot (the subscriber's `get_subscription`
  call is the live check).
- `google_application_credentials_path`: when set, must be an existing file
  at the path, owner-readable, mode `0600`. The subscriber enforces this at
  boot.
- `subscriber_heartbeat_interval_seconds`, `subscriber_heartbeat_staleness_factor`:
  positive floats.
- Cross-field: if any of `google_pubsub_subscription` or
  `google_application_credentials_path` is set, all must be set; otherwise
  Gmail push is "off" and the sidecar refuses to start (clean fail-loud).

### Renamed → deleted (P2)

- `google_provider_event_url` is **deleted**. The Calendar watch's `address`
  field is constructed in code as
  `f"{settings.public_webhook_base_url}/v1/providers/google/events?resource_type=calendar&resource_id={resource_id}"`.
- `google_oauth_redirect_uri` default changes from `http://127.0.0.1:8000/...`
  to a computed `f"{settings.public_webhook_base_url}/v1/connectors/google/callback"`
  in production. The setting remains overridable for local dev (where
  `public_webhook_base_url` is unset and the loopback default applies).

### Unchanged

`google_provider_event_token`, `google_pubsub_topic`,
`provider_reconcile_sync_interval_seconds`, all OAuth client id/secret
settings.

### `.env.local` end state for the prod VPS

```
ARIEL_DEPLOYMENT_MODE=production
ARIEL_PUBLIC_WEBHOOK_BASE_URL=https://ariel.nielseriknandal.com
ARIEL_GOOGLE_OAUTH_CLIENT_ID=...
ARIEL_GOOGLE_OAUTH_CLIENT_SECRET=...
ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN=<32+ url-safe random chars>
ARIEL_GOOGLE_PUBSUB_TOPIC=projects/<project>/topics/ariel-gmail-watch
ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/ariel-gmail-watch-sub
ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH=/home/niels/src/personal/ariel/.secrets/gcp-pubsub-sa.json
# … rest unchanged
```

The legacy `ARIEL_GOOGLE_PROVIDER_EVENT_URL` line is removed.

## Files

### Added

- `docs/modules/google-workspace-push-cutover.md` — this document.
- `deploy/caddy/Caddyfile` — the reverse-proxy block above.
- `deploy/caddy/install.sh` — idempotent installer: apt-installs Caddy from the
  official Cloudsmith repo, drops the Caddyfile, opens UFW 80/443,
  `systemctl enable --now caddy`. ~30 lines. Re-runnable.
- `deploy/systemd/ariel-pubsub.service` — the sidecar unit. Modeled on
  `ariel-discord.service`, with `After=` adjusted.
- `scripts/gcp_provision_pubsub.sh` — `gcloud`-based idempotent provisioning:
  creates the topic, subscription with `--enable-exactly-once-delivery`,
  DLQ topic, DLQ subscription, IAM bindings (Publisher on topic for
  `gmail-api-push@system.gserviceaccount.com`, Publisher on DLQ for the
  Pub/Sub service agent, Subscriber + Viewer on source sub for the runtime SA,
  Subscriber on DLQ sub for the runtime SA). Inputs: `GCP_PROJECT`,
  `RUNTIME_SA_EMAIL`. Outputs: full resource paths to paste into `.env.local`.
- `scripts/gcp_create_runtime_sa.sh` — `gcloud`-based: creates the runtime
  service account, creates a key, writes it to
  `~/.ariel-secrets/gcp-pubsub-sa.json` with `chmod 600`. Re-running rotates
  the key and warns the operator to update the deployment.
- `src/ariel/pubsub_subscriber.py` — the sidecar entrypoint:
  `main()`, `_on_message(message)`, `_emit_heartbeat()`, `_run()`. ~250 lines
  estimated.
- `alembic/versions/<timestamp>_subscriber_heartbeat.py` — adds the
  `subscriber_heartbeat` table.
- `tests/fake_pubsub.py` — `FakePubSubMessage`, `FakeSubscriberClient`,
  `FakeStreamingPullFuture`, `FakeAckResponse`. Plain-Python in-memory fakes
  matching the codebase's existing fake style
  (`tests/integration/test_provider_ingestion_p3.py`'s `WatchRecordingProvider`).
- `tests/integration/test_pubsub_subscriber.py` — end-to-end tests against
  the fake: happy path (insert + enqueue + ack), redelivery (same messageId →
  duplicate=true), malformed payload (nack path), unknown account (ack and
  drop), subscriber-down → `/v1/health` returns 503.

### Edited

- `src/ariel/config.py` — additions in "Settings End State / Added" and the
  rename/deletion described in "Renamed → deleted". Validators wired.
- `src/ariel/app.py`:
  - `POST /v1/providers/google/events` (`~3633-3795`) — per-channel HMAC
    layered on top of the global gate; `resource_type=gmail|drive` rejected
    with 400; channel-id lookup added; tests touched accordingly.
  - `GET /v1/health` — read `subscriber_heartbeat` and project subscriber
    state into the response; return 503 on staleness.
  - The `app.state.google_provider_event_token` initialization is unchanged.
  - The Calendar `address` URL is no longer read from
    `google_provider_event_url`; the new accessor lives on
    `AppSettings` and is consumed in `google_connector.py`.
- `src/ariel/google_connector.py`:
  - `register_provider_watches` (`~5074-5111`) — for Calendar, build `address`
    from `settings.public_webhook_base_url`.
  - The `pubsub_topic=` value passed to `gmail_register_watch` continues to
    come from `settings.google_pubsub_topic`; no functional change.
- `src/ariel/worker.py`:
  - `_PROVIDER_WATCH_RENEW_LEAD_SECONDS` (`~207`) — change `24 * 3600` →
    `6 * 24 * 3600` (6 days) for daily-cadence Gmail renewal compliance.
  - The 6-hour sweep interval at `~173` is unchanged.
- `src/ariel/persistence.py` — `SubscriberHeartbeatRecord` model added.
- `.env.example` — add the three new env vars with comments; **remove**
  `ARIEL_GOOGLE_PROVIDER_EVENT_URL`.
- `docs/production-runbook.md` — the Google Workspace section rewritten:
  GCP project setup, DNS A-record, Caddy install, provisioning script,
  service account, env vars, smoke test.
- `docs/modules/proactivity.md` — the "Provider ingestion" section reflects
  the per-channel HMAC, the DLQ, the sidecar, and the daily Gmail renewal.
- `docs/modules/index.md` — add the new cutover doc.
- `README.md` — only if it advertised push as "in progress"; mark it shipped
  with one line referencing this cutover doc.
- Tests touching the existing webhook handler (e.g.,
  `tests/integration/test_discord_primary_durable_workflows_acceptance.py`
  Google-provider-event-ingress tests) — extended for per-channel HMAC.

### Deleted

- The `ARIEL_GOOGLE_PROVIDER_EVENT_URL` field on `AppSettings` and its
  validator.
- The `.env.example` line for `ARIEL_GOOGLE_PROVIDER_EVENT_URL`.
- The "shared-token-only" code branch on `POST /v1/providers/google/events`:
  after P2, a request that passes the global gate but does not match a
  per-channel record's `channel_token` is rejected. There is no fallback path.

## The Cutover

Five phases. Each independently shippable; `make verify` green; alembic up/down
clean.

### P1 — Settings and operator infra

Additive surface only. No runtime behavior changes. Lands first because the
later phases consume the new settings.

- Add `public_webhook_base_url`, `google_pubsub_subscription`,
  `google_application_credentials_path`,
  `subscriber_heartbeat_interval_seconds`,
  `subscriber_heartbeat_staleness_factor` to `AppSettings` with validators.
- Add `subscriber_heartbeat` table + alembic migration.
- Add `SubscriberHeartbeatRecord` to `persistence.py`.
- Add `deploy/caddy/Caddyfile`, `deploy/caddy/install.sh`,
  `scripts/gcp_provision_pubsub.sh`, `scripts/gcp_create_runtime_sa.sh`.
- Rewrite the Google Workspace section of `docs/production-runbook.md` to the
  end-state runbook (DNS, Caddy, provisioning, env vars, smoke test).
- Tests: validator tests (`tests/unit/test_config.py`) for the five new
  fields; migration up/down test.

P1 is operator-ready for the box without changing any wire protocol.

### P2 — API surface cutover

The hard cutover of the inbound HTTP layer.

- Add per-channel HMAC validation to `POST /v1/providers/google/events`:
  look up `ProviderWatchChannelRecord` by `X-Goog-Channel-ID`; reject 401 if
  missing; `hmac.compare_digest` against `channel_token`; reject 401 if
  mismatch. This runs after the global-token gate, before the dedup logic.
- Reject `resource_type=gmail|drive` on this endpoint with 400.
- Delete `google_provider_event_url` from `AppSettings`, `.env.example`, and
  every reference site.
- Construct the Calendar watch `address` from
  `settings.public_webhook_base_url` in `google_connector.py`.
- In production mode, validate that `public_webhook_base_url` is set; refuse
  to boot otherwise (existing production-mode validators model in
  `config.py:201-222`).
- Tests:
  - Per-channel HMAC happy path, channel-not-found 401, wrong-token 401,
    cross-channel-token-replay 401 (channel A's token does not authenticate
    channel B).
  - `resource_type=gmail` returns 400.
  - All existing dedup + conflict tests stay green.
  - One acceptance test against `register_provider_watches` confirms the
    constructed `address` matches the expected pattern.

After P2 the Calendar push path is production-safe with two-layer HMAC.
Gmail push is still off (the subscriber sidecar lands in P3).

### P3 — Pub/Sub subscriber sidecar

The Gmail live-push layer.

- Add `google-cloud-pubsub` to `pyproject.toml` (`>=2.38`).
- Write `src/ariel/pubsub_subscriber.py` per "The Gmail Pub/Sub path" above:
  `main()` reads settings, asserts SA file mode 0600, builds
  `SubscriberClient`, runs `get_subscription`, wires the streaming pull,
  installs a SIGTERM handler that drains in-flight callbacks via
  `StreamingPullFuture.cancel()` + `result(timeout=30)`, and writes a
  heartbeat row every 30s.
- Add `deploy/systemd/ariel-pubsub.service`.
- Extend `GET /v1/health` to surface the heartbeat row.
- Tests:
  - `tests/fake_pubsub.py` with the fakes named above.
  - `tests/integration/test_pubsub_subscriber.py`:
    - Happy path — a `FakePubSubMessage` triggers one `ProviderEventRecord` +
      one `provider_event_received` task; `ack_with_response()` was called
      and resolved SUCCESS.
    - Redelivery — the same messageId arrives twice; second insert dedups;
      task enqueued once.
    - Malformed payload — message data is not valid UTF-8 JSON; nack is
      called; no DB write.
    - Unknown account — `emailAddress` not in `GoogleConnectorRecord`; ack is
      called; no DB write.
    - Heartbeat staleness → `/v1/health` returns 503.
    - SIGTERM drain — pending callbacks complete before exit.
- The first `make verify` after P3 lands runs without the Pub/Sub SDK
  reaching real GCP; the fakes carry the test path.

### P4 — Daily Gmail renewal cadence

Single-line behavioral change.

- Change `worker.py` `_PROVIDER_WATCH_RENEW_LEAD_SECONDS` from `24 * 3600` to
  `6 * 24 * 3600`. With the existing 6-hour sweep, this means every renewal
  pass renews any watch with less than 6 days remaining — i.e. the renewal
  runs daily under the 7-day expiry cap, matching Google's published
  recommendation.
- Update the relevant test in `tests/integration/test_provider_ingestion_p3.py`
  (the "renewal scheduling when channel expires within 24h" test becomes
  "within 6 days").

### P5 — Doc finalization

Docs-only. Lands after P1–P4 have shipped.

- Rewrite the "Provider ingestion" section of `docs/modules/proactivity.md`
  to the cutover's end state: per-channel HMAC, the DLQ, the sidecar, the
  daily Gmail cadence.
- Add the new cutover doc to `docs/modules/index.md`.
- Update `README.md` only if it carried a stale "push not yet shipped"
  reference.
- Add a "Cutover Status: completed YYYY-MM-DD (commit <sha>)" footer to this
  document.
- Update `keys-and-identities.md` with the rule "service-account JSON files
  live on the filesystem, chmod 600, owned by the runtime user; the
  `ConnectorTokenCipher`-encrypted token table holds per-user OAuth refresh
  tokens only." — codifying the new credential boundary.

## Hard-Cutover Decisions

- **Single FQDN, single Caddy block.** `ariel.nielseriknandal.com` carries
  the entire Google integration: webhook receiver and OAuth callback. There
  is no second domain, no path-prefix split, no alternate vhost.
- **Per-channel HMAC is mandatory.** The global token is a coarse gate;
  per-channel HMAC is the authentication. A channel with no record cannot
  authenticate.
- **Pull, not push, for Gmail.** The webhook handler is Calendar-only.
  Pub/Sub never touches the HTTPS path. This avoids JWT-OIDC validation on
  the webhook (a feature we would otherwise have to introduce) and keeps
  the public surface narrow.
- **Exactly-once subscription.** Pub/Sub volume is low enough (≤1 evt/sec/user)
  that the small latency cost is invisible, and exactly-once is the only
  subscription mode where Google guarantees the ack deadline.
- **Service-account JSON on disk.** First "act as the app" credential in the
  codebase; it lives on the filesystem, chmod 600, not in the encrypted-token
  table. The token-cipher table remains user-OAuth-only.
- **DLQ for Gmail Pub/Sub; no DLQ for Calendar HTTPS.** Pub/Sub is the
  delivery layer that needs one; Calendar's existing dedup + Google's own
  retry/backoff is sufficient.
- **One sidecar process, supervised by systemd.** Not embedded in
  `ariel-worker` (proactivity.md's "one serialized worker" rule); not
  embedded in `ariel-api` (transport.md's "transport ≠ work lifetime" rule).
- **Provisioning is operator-owned.** The sidecar verifies, never creates.
  The provisioning scripts are out-of-band, runnable by the operator with
  their own credentials.
- **No HTTP-pull Pub/Sub model.** Pub/Sub messages never POST to the webhook.
- **No multi-tenant subscription topology.** One topic, one subscription.
- **No `notifications` table, no `push_status` table.** `subscriber_heartbeat`
  is the only new operational-state row.
- **No compatibility layer.** `ARIEL_GOOGLE_PROVIDER_EVENT_URL` is deleted.
  Single-shared-token-only validation is deleted. The runbook's "old" Google
  Workspace section is replaced, not augmented.

## Acceptance Criteria

- DNS A-record `ariel.nielseriknandal.com` → VPS public IP exists; Caddy is
  installed via `deploy/caddy/install.sh` and `caddy.service` is active.
  `curl -I https://ariel.nielseriknandal.com/` returns `404`.
  `curl -I https://ariel.nielseriknandal.com/v1/providers/google/events`
  returns `405` (GET on a POST-only endpoint) — i.e., the path is forwarded.
- `scripts/gcp_provision_pubsub.sh` runs idempotently and creates: the
  `ariel-gmail-watch` topic with Publisher binding for
  `gmail-api-push@system.gserviceaccount.com`; the
  `ariel-gmail-watch-sub` subscription with exactly-once enabled and the
  DLQ wired; the `ariel-gmail-watch-dlq` topic with Publisher binding for
  the Pub/Sub service agent; the `ariel-gmail-watch-dlq-sub` subscription.
- `scripts/gcp_create_runtime_sa.sh` runs idempotently and produces a chmod
  600 SA JSON the subscriber loads at boot.
- `POST /v1/providers/google/events` with no per-channel record matching
  `X-Goog-Channel-ID` returns 401 even with the correct global token.
- `POST /v1/providers/google/events` with `resource_type=gmail` is rejected
  at the FastAPI boundary with `422` (the `Literal["calendar"]` query-param
  type narrows gmail/drive out).
- A Calendar push delivered through Caddy: `ProviderEventRecord` row
  inserted, `provider_event_received` task enqueued, no errors logged.
- A Gmail message in a connected account: within ≤10s, the subscriber sidecar
  has acked one Pub/Sub message, inserted one `ProviderEventRecord` row,
  enqueued one `provider_event_received` task, and the worker has run
  `process_provider_sync_due` and advanced `SyncCursorRecord.cursor_value`.
- `GET /v1/health` returns the subscriber heartbeat block; killing the
  sidecar causes the endpoint to return 503 within `2 * 30s`.
- The Gmail watch is registered with `ARIEL_GOOGLE_PUBSUB_TOPIC` set; renewal
  fires every 6-hour sweep for any watch with <6 days remaining;
  `worker.py:_PROVIDER_WATCH_RENEW_LEAD_SECONDS == 6 * 24 * 3600`.
- `ARIEL_GOOGLE_PROVIDER_EVENT_URL` is absent from `config.py`, `.env.example`,
  the runbook, and the codebase grep.
- The four systemd units (`ariel-api`, `ariel-worker`, `ariel-discord`,
  `ariel-pubsub`) plus `caddy.service` are all `active (running)` after a box
  reboot.
- `ruff check`, `ruff format --check`, `mypy src tests`, and the full
  `pytest` suite all pass at every phase; alembic upgrades and downgrades
  cleanly.
- The cutover doc footer records "Cutover Status: completed YYYY-MM-DD
  (commit `<sha>`)" once P5 lands.

## Risks

- **Pub/Sub SA leakage.** The runtime SA JSON file on disk is a high-value
  credential. Mitigation: chmod 600, owned by `niels`, stored under a
  gitignored path; the provisioning script grants the runtime SA only
  Subscriber on the subscription and the DLQ subscription. P5's
  `keys-and-identities.md` update names the file as a tracked sensitive
  artifact.
- **Public surface drift.** Caddy is the only thing standing between the
  public internet and the loopback app. A Caddyfile typo could expose more
  than intended. Mitigation: the Caddyfile has a default-404 `handle` block;
  the provisioning script `caddy validate`s before reload; the runbook
  prescribes a post-install probe (404 on `/`, 405 on the webhook path).
- **Subscriber silently stops.** A crashed sidecar with `Restart=always` will
  loop-crash and the user sees nothing until they check Discord. Mitigation:
  `subscriber_heartbeat` staleness → `/v1/health` 503; renewal failures
  already enqueue an `agent_wake` (`worker.py:~247-266`). The reconcile poll
  remains the backstop.
- **DLQ growth ignored.** Messages persistently dead-lettered indicate a
  schema bug. Mitigation: P5's runbook adds a Cloud Monitoring alert on
  `subscription/dead_letter_message_count > 0` and a `gcloud pubsub
  subscriptions seek` recipe for the operator.
- **OAuth redirect URI mismatch.** The OAuth client must list
  `https://ariel.nielseriknandal.com/v1/connectors/google/callback`. A
  mismatch fails connect-time. Mitigation: P1 runbook documents the GCP
  console steps; the OAuth client setup is a one-time operator action.
- **Cert issuance failure on first boot.** ACME HTTP-01 requires port 80 open
  to the public, A-record propagated, and no port collision. Mitigation:
  install.sh checks for port 80 collisions; runbook prescribes verifying DNS
  propagation before running install.
- **`get_subscription` permissions.** The runtime SA needs Subscriber, not
  Editor; provisioning grants exactly Subscriber. A misconfigured grant
  shows up as a clear permission error at sidecar boot, not as silent data
  loss.

## Operator Runbook Outline (end state, lives in `docs/production-runbook.md`)

1. **Prerequisites**
   - VPS: Hetzner CPX11 `dev-server-cpx11`, Ubuntu 24.04, ports 80/443 open
     in the Hetzner firewall.
   - GCP project: any project with billing; the operator's user has
     Owner/Editor and `roles/pubsub.admin`.
   - DNS: A record `ariel.nielseriknandal.com` → VPS public IPv4, propagated.

2. **Install Caddy**
   - `sudo bash deploy/caddy/install.sh`
   - Verify: `systemctl status caddy`, then
     `curl -I https://ariel.nielseriknandal.com/` → 404 expected.

3. **Provision Pub/Sub**
   - `export GCP_PROJECT=<your-project>`
   - `bash scripts/gcp_create_runtime_sa.sh` → writes
     `~/.ariel-secrets/gcp-pubsub-sa.json`.
   - `bash scripts/gcp_provision_pubsub.sh` → prints the topic and
     subscription full resource paths.

4. **Configure `.env.local`**
   - Fill in `ARIEL_PUBLIC_WEBHOOK_BASE_URL`,
     `ARIEL_GOOGLE_OAUTH_CLIENT_ID/SECRET`,
     `ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN` (`python -c 'import secrets;
     print(secrets.token_urlsafe(32))'`),
     `ARIEL_GOOGLE_PUBSUB_TOPIC`, `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION`,
     `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH`.

5. **OAuth client console step**
   - Add `https://ariel.nielseriknandal.com/v1/connectors/google/callback` to
     the OAuth client's authorized redirect URIs.

6. **Install and start the systemd units**
   - `sudo cp deploy/systemd/*.service /etc/systemd/system/`
   - `sudo systemctl daemon-reload`
   - `sudo systemctl enable --now ariel-api ariel-worker ariel-discord ariel-pubsub`

7. **Connect a Google account**
   - From Discord, run the connect command. Browser opens to Google consent;
     redirect lands on the public callback.
   - On callback success, the worker registers a Gmail watch and a Calendar
     watch; `ProviderWatchChannelRecord` rows appear.

8. **Smoke test**
   - Calendar: create an event in the connected calendar. Within ≤30s,
     `provider_events` shows a new row; `agent_wake` fires; Ariel posts to
     Discord.
   - Gmail: send an email to the connected mailbox. Within ≤30s, same.

9. **Operational checks**
   - `curl http://127.0.0.1:8000/v1/health` shows `subscribers.gmail_pubsub`
     with recent `last_seen_at`.
   - Cloud Monitoring alerts on `oldest_unacked_message_age > 5m`,
     `expired_ack_deadlines_count > 1%`, `dead_letter_message_count > 0`.

## References / Source Findings

This plan distills the May 2026 push-readiness SME survey (seven parallel
sub-agents over Gmail/Calendar watch code, the webhook receiver, watch
renewal, tests, docs, deployment, and credential/observability conventions),
plus the four-agent design pass (Pub/Sub SOTA, Caddy production patterns,
worker plug-in topology, repo cross-cutting rules), against the following
authoritative sources current as of May 2026:

- Pub/Sub: [subscribe-best-practices](https://docs.cloud.google.com/pubsub/docs/subscribe-best-practices),
  [exactly-once-delivery](https://docs.cloud.google.com/pubsub/docs/exactly-once-delivery),
  [lease-management](https://docs.cloud.google.com/pubsub/docs/lease-management),
  [handling-failures (DLQ)](https://docs.cloud.google.com/pubsub/docs/handling-failures),
  [flow-control-messages](https://docs.cloud.google.com/pubsub/docs/flow-control-messages),
  [monitoring](https://docs.cloud.google.com/pubsub/docs/monitoring),
  [pull subscriptions](https://docs.cloud.google.com/pubsub/docs/pull).
- Gmail API: [push notifications](https://developers.google.com/workspace/gmail/api/guides/push),
  [users.watch](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch).
- Calendar API: [push notifications](https://developers.google.com/workspace/calendar/api/guides/push).
- IAM: [service-account key best practices](https://docs.cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys),
  [workload identity federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation).
- Caddy: [reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
  [handle](https://caddyserver.com/docs/caddyfile/directives/handle),
  [automatic HTTPS](https://caddyserver.com/docs/automatic-https),
  [install](https://caddyserver.com/docs/install).

The plan also inherits repo-internal rules from `docs/ai-first.md`,
`docs/simplicity.md`, `docs/boundaries.md`, `docs/concurrency.md`,
`docs/correctness.md`, `docs/control-flow.md`, `docs/keys-and-identities.md`,
`docs/operation-types.md`, `docs/mutation-ordering.md`,
`docs/modules/proactivity.md`, `docs/modules/transport.md`, and the
schema-consolidation cutover that deferred this work in the first place.

## Departures From Spec During Implementation

Two small spec sentences were not implemented as written, in favor of repo
conventions cited inline:

- The spec's `google_application_credentials_path` validator described
  filesystem-existence and `chmod 600` checks at config-load time.
  Implementation keeps the validator string-format-only (absolute path,
  non-blank) and pushes the existence + mode check to the subscriber's boot
  path (`pubsub_subscriber.main`). Rationale: `docs/boundaries.md` —
  filesystem state is validated at the boundary that uses it, not at config
  load (which would also break tests that don't materialize the file).
- The spec described auto-derivation of `google_oauth_redirect_uri` from
  `public_webhook_base_url` in production. Implementation does not
  auto-derive; the operator sets both env vars explicitly, and the
  production runbook documents both. Rationale: minimalism — auto-derivation
  is a second code path that the operator-runbook step already covers.
- Reject of `resource_type=gmail|drive` on the webhook is enforced by
  narrowing the FastAPI query-param `Literal` to `["calendar"]`. FastAPI
  returns `422` (validation error) instead of the spec's `400`. The
  rejection semantics are unchanged; only the status code differs.
- `docs/keys-and-identities.md` was not extended with the SA-on-disk rule.
  That document covers identifier *naming* conventions, not credential
  storage; the SA-on-disk boundary is documented in this cutover and in
  `docs/production-runbook.md`'s Google Workspace Push section.

---

Cutover Status: completed 2026-05-19. P1–P5 merged. `make verify` (ruff +
format + mypy + pytest) green at every phase. The Gmail/Calendar live-push
path is operational once the operator runbook (DNS, Caddy, gcloud
provisioning scripts, env vars, OAuth redirect URI) is followed.
