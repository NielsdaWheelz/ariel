# Proactivity

## Scope

This document owns Ariel's proactivity: the agent loop reached by non-human
triggers, the durable scheduler that holds future wakes, the schedule syscall,
and provider push and poll ingestion.

Proactivity is not a subsystem. It is the main agent loop, reached by
non-human triggers, plus one durable scheduler. There is no ambient pipeline,
no case, no observation record, no deliberation subagent, and no proactive
decision table.

Proactivity follows [../ai-first.md](../ai-first.md): the model owns every
judgment — whether an event matters, whether to interrupt, what to do, when to
look again; deterministic code owns only rails — the queue, ingress
normalization, provider auth, capability policy, approval, and delivery.

## The wake model

There is one agent-loop entrypoint, `_wake`, a module-level function in
`app.py`. Every trigger invokes it. It takes a `WakeContext` and a `Runtime`,
assembles memory and eligibility context, runs the answer model with the `run`
tool, executes the program, and emits any output.

A `WakeContext` carries `trigger_kind` (`user_message`, `scheduled_task`,
`provider_sync`, or `research_completion`), `prompt_text`, `discord_context`,
`attachment_sources`, and `ingress_provenance`. The trigger kind is the only
thing that distinguishes a proactive wake from a user turn.

A proactive wake is a normal turn. It receives the same `run` tool and the same
memory faculties — the retriever and rememberer run as on any turn. A wake may
finish with `agent.finish_silent`, producing no delivery; that finalization
emits `evt.agent.finished_silent` with the reason, so operators can query
proactive silence directly (see [agent-loop.md](agent-loop.md)). Every wake is
recorded as a turn.

## Triggers

Wake sources reach `_wake` through the worker:

- **A user message** — Discord or API.
- **A provider push event** — a Gmail or Calendar `watch` callback. The
  webhook or Pub/Sub sidecar verifies and normalizes the event, enqueues
  `provider_event_received`, and the worker may enqueue a wake after sync finds
  provider data that should be reviewed.
- **An Agency job event** — the webhook verifies and normalizes the event,
  enqueues `agency_event_received`, and the worker enqueues a wake for job
  states the agent should review.
- **A poll result** — the periodic provider reconcile sync applies the same
  wake rules as push ingestion.
- **A due scheduled task** — an `agent_wake` row whose `run_after` has arrived.
- **A research completion** — when a `research_run` task finishes, the worker
  enqueues an `agent_wake` carrying the typed `research_finding` payload. The
  main agent wakes with `trigger_kind = research_completion`, reads the finding
  (carried with tainted provenance), and answers the user.

A Google connector error also enqueues a wake, so the user learns of a broken
connector. There is no periodic sweep of internal tables for candidates;
external events enter durable ingestion tasks, and wakes are enqueued when the
worker has actionable data or status for the agent.

## The scheduler (`background_tasks`)

`background_tasks` is the one durable task queue. It is shared infrastructure,
not proactivity-exclusive. This section documents the queue contract;
`BackgroundTaskRecord` implements the row shape and allowed task types, and
`worker.py` owns dispatch behavior. Module docs own module-specific task
semantics.

A row is `id`, `task_type`, `idempotency_key`, `provider_write_receipt_id`,
`payload`, `attempts`, `recurrence_seconds`, `run_after`, `created_at`, and
`updated_at`. Plain note wakes carry an `agent_wake` payload with `note`.
Provider-sync wakes carry `kind='provider_sync_review'`, sync metadata, and
bounded changed-item evidence from normalized provider reads. Research-completion
wakes carry a typed `research_finding`. The worker rejects any
`agent_wake` payload that is none of these shapes.

The single-threaded worker takes the earliest due row, dispatches by
`task_type`, and on success deletes the row, or — when `recurrence_seconds` is
set — re-arms it in place to its next occurrence. A row is deleted only on
success; a crash mid-wake leaves the row to retry. A failed task backs off
within `attempts` (cap 5); on exhaustion a one-shot is abandoned and a
recurring task is re-armed. There is no claim protocol, heartbeat, dead-letter
state, or stale-task reaper: a row existing and due is the only pending state.
Effects that must not repeat carry an idempotency key in the capability layer.

## The schedule syscall

The `run` program's entire scheduling surface is one syscall,
`proactive.schedule(when, note)`, backed by the `cap.proactive.schedule`
capability. It is `allow_inline`. The name is dotted because the sandbox
rejects single-segment syscall names.

`when` is an RFC3339 timestamp — a one-shot wake. The syscall writes one
`agent_wake` row: `run_after` from `when`, `payload` from `note`. Recurrence is
the agent re-scheduling itself on each wake, not a recurrence field on the
syscall. A user reminder, a "check back on this later," and a recurring routine
are each the agent calling `proactive.schedule`.

The agent never writes the queue directly; it only calls the syscall.

## Provider ingestion

A `provider_watch_channels` table records push-channel identity, expiry, and
local revocation state.
When a Google connector connects, Ariel registers a Gmail `users.watch` (Cloud
Pub/Sub) channel and a Calendar `events.watch` channel.

Calendar push and Gmail push arrive on different paths but converge on the same
durable artifact:

- **Calendar push** — Google POSTs to `/v1/providers/google/events` over the
  public Caddy-fronted HTTPS endpoint. The handler validates the
  `X-Goog-Channel-Token` against the per-channel `channel_token` stored on
  the active, unexpired `provider_watch_channels` row for the requested
  resource and channel. Disconnect deletes these rows, so stale channel
  callbacks fail before enqueue. A missing or mismatched per-channel token
  returns 401. On accept, one `ProviderEventRecord` row is inserted and one
  `provider_event_received` background task is enqueued.
- **Gmail push** — Google publishes to a Cloud Pub/Sub topic; the
  `ariel-pubsub` sidecar systemd unit consumes the matching subscription via
  StreamingPull with exactly-once delivery and a dead-letter topic. On each
  delivery it inserts the same `ProviderEventRecord` row and enqueues the same
  `provider_event_received` task — and only then acks the Pub/Sub message.
  Malformed payloads, unknown accounts, and inactive connector accounts ack and
  drop because redelivery cannot repair immutable provider data. The sidecar
  writes a `subscriber_heartbeat` row that `/v1/health` reports.

The worker performs two recurring maintenance tasks from connector state:

- `provider_watch_renew_due` re-arms active watches for a connected connector
  before they expire. The
  6-hour sweep + 6-day lead time renews any watch with less than 6 days
  remaining, matching Google's recommended daily Gmail cadence under the
  7-day cap.
- `provider_reconcile_sync_due` runs the reconcile poll, the baseline that is
  independent of push.

A stale delta cursor — a Gmail `404` or a Calendar `410` — clears the cursor
and triggers a full resync. A provider push event enqueues ingestion work.
Provider sync always updates cursors and local evidence for supported deltas.
Gmail sync enqueues an `agent_wake` only for new inbound messages; label
changes, deletions, sent-only changes, and draft/outbound changes do not wake
the agent. Calendar sync may wake on new or changed event data. A wake carries
bounded, tainted review context and still dispatches to the shared `_wake` loop;
there is no separate Discord path.

## Delivery

Every turn — user reply, proactive wake, research completion — is delivered by
the same worker-side path. After a turn commits, the worker posts the emitted
message to the user's Discord channel over the Discord REST API
(`discord_channel_id`, `discord_bot_token`,
`discord_notification_timeout_seconds`). A wake that originates from a Discord
message posts as a reply to it; a wake without one posts to the default channel.
A wake that ends without emitting is not delivered. There is no `notifications`
table: Discord is the record of what was sent, and every wake is a turn.

## Autonomous action

A wake may call any capability it is eligible for. The gate on dangerous action
is the per-capability `requires_approval` policy: every high-impact,
irreversible, or externally-visible capability routes to a Discord approval the
user confirms; low-impact capabilities run inline. There is no `autonomy_scopes`
table — autonomy is initiative, not pre-authorization.

Because a wake can run on tainted input — an email carrying a prompt injection —
the `requires_approval` policy is the security boundary. A tainted-input wake
fooled into proposing a harmful action produces an approval prompt the user
denies; it cannot act irreversibly on its own.

## Rules

- One entrypoint, `_wake`, serves every trigger. There is no separate proactive
  cognition path, deliberation subagent, or proactive-decision record.
- A proactive wake has the same `run` tool and memory as a user turn and may
  finish silently with `agent.finish_silent`.
- `background_tasks` is the only queue, timer, and scheduler. Proactivity adds
  no table of its own beyond `provider_watch_channels`.
- The worker takes the earliest due row and deletes it on success. There is no
  claim protocol, heartbeat, dead-letter state, or reaper.
- Wake tasks that have already committed a source turn are replayed through
  `turns.source_background_task_id`; a retry does not call the model a second
  time for the same task row.
- The agent's scheduling surface is `proactive.schedule`; it writes scheduled
  `agent_wake` rows through the capability runtime. System-owned wake writers
  are Gmail syncs that find new inbound messages, Calendar syncs that find new
  or changed event data, Google connector-error handling, Agency job event
  handling, and research-run completion.
- Recurrence is the agent re-scheduling itself; the syscall takes a one-shot
  timestamp.
- Delivery is one code path: the worker posts the emitted message to Discord
  after the turn commits. There is no `notifications` table and no
  proactivity-specific delivery, audit, or feedback table.
- Per-capability `requires_approval` is the autonomous-action boundary. There
  is no `autonomy_scopes` table or standing-grant system.
- Commitment tracking, work follow-ups, leave-by, and email thread-watching are
  emergent agent behavior built from calendar and email access, the maps
  capability, `proactive.schedule`, and memory — not coded subsystems.
- New proactive machinery — an ambient pipeline, a triage tier, a cheap-model
  pre-filter, a second queue — is forbidden. If wake volume ever bites, the
  lever is deterministic coalescing of a burst into one wake, a rail.
