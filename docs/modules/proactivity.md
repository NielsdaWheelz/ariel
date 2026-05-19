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
normalization, provider auth, capability policy, approval, and delivery. The
cutover that produced this design is recorded in
[proactivity-cutover.md](proactivity-cutover.md).

## The wake model

There is one agent-loop entrypoint, `_wake`, a module-level function in
`app.py`. Every trigger invokes it. It takes a `WakeContext` and a `Runtime`,
assembles memory and eligibility context, runs the answer model with the `run`
tool, executes the program, and emits any output.

A `WakeContext` carries `trigger_kind` (`user_message`, `scheduled_task`, or
`research_completion`), `prompt_text`, `discord_context`, `attachment_sources`,
and `ingress_provenance`. The trigger kind is the only thing that distinguishes
a proactive wake from a user turn.

A proactive wake is a normal turn. It receives the same `run` tool and the same
memory faculties — the retriever and rememberer run as on any turn. A wake may
end without emitting; "do nothing" is an empty turn, not a recorded decision.
Every wake is recorded as a session turn.

## Triggers

Five triggers wake the agent, all through `_wake`:

- **A user message** — Discord or API.
- **A provider push event** — a Gmail or Calendar `watch` callback, or an
  Agency job event. The webhook verifies and normalizes the event and enqueues
  a wake.
- **A poll result** — the periodic provider reconcile sync finds new or changed
  data and enqueues a wake.
- **A due scheduled task** — an `agent_wake` row whose `run_after` has arrived.
- **A research completion** — when a `research_run` task finishes, the worker
  enqueues an `agent_wake` carrying the typed `research_finding_v1`. The main
  agent wakes with `trigger_kind = research_completion`, reads the finding
  (carried with tainted provenance), and answers the user.

A Google connector error also enqueues a wake, so the user learns of a broken
connector. There is no periodic sweep of internal tables for candidates; each
event wakes the agent when it happens.

## The scheduler (`background_tasks`)

`background_tasks` is the one durable task queue. It is shared infrastructure:
besides agent wakes it carries the memory rememberer and sweep, durable action
execution, approval expiry, and Agency and provider ingestion tasks. It is not
proactivity-exclusive.

A row is `id`, `task_type`, `idempotency_key`, `provider_write_receipt_id`,
`payload`, `attempts`, `recurrence_seconds`, `run_after`, `created_at`, and
`updated_at`. `task_type` is the discriminator the worker dispatches on. An
agent wake is a `task_type='agent_wake'` row whose `payload` carries the
AI-authored note — the agent's message to its future self, no schema, the same
kind of artifact as a memory fact.

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

A `provider_watch_channels` table records push-channel identity and expiry.
When a Google connector connects, Ariel registers a Gmail `users.watch` (Cloud
Pub/Sub) channel and a Calendar `events.watch` channel.

The worker performs two recurring maintenance tasks from connector state:

- `provider_watch_renew_due` re-arms each `watch` before it expires — Gmail
  daily, Calendar before its ~7-day limit.
- `provider_reconcile_sync_due` runs the reconcile poll, the baseline that is
  independent of push.

A stale delta cursor — a Gmail `404` or a Calendar `410` — clears the cursor
and triggers a full resync. A provider push event and a sync that finds new
data each enqueue an `agent_wake`.

## Delivery

Every turn — user reply, proactive wake, research completion — is delivered by
the same worker-side path. After a turn commits, the worker posts the emitted
message to the user's Discord channel over the Discord REST API
(`discord_channel_id`, `discord_bot_token`,
`discord_notification_timeout_seconds`). A wake that originates from a Discord
message posts as a reply to it; a wake without one posts to the default channel.
A wake that ends without emitting is not delivered. There is no `notifications`
table: Discord is the record of what was sent, and every wake is a session turn.

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
  end without emitting.
- `background_tasks` is the only queue, timer, and scheduler. Proactivity adds
  no table of its own beyond `provider_watch_channels`.
- The worker takes the earliest due row and deletes it on success. There is no
  claim protocol, heartbeat, dead-letter state, or reaper.
- The agent's scheduling surface is `proactive.schedule`. The one other code
  path that writes `agent_wake` rows is the research-run completion handler,
  which enqueues the finding wake on behalf of the system — not the agent.
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
