# Proactivity Crystallization Cutover

## Scope

This document is the hard-cutover plan that crystallizes Ariel's proactivity
into its essential form. It converts the May 2026 SME survey and the
architecture review that followed into an implementation spec.

It owns the cutover only. The standing design lands in
[proactivity.md](proactivity.md), written by Phase 5. The plan inherits
[../ai-first.md](../ai-first.md), [../cleanliness.md](../cleanliness.md), and
[../simplicity.md](../simplicity.md), and follows the memory module's rule:
delete the machinery, keep a model and a thin rail.

The cutover is hard: no compatibility layer, no dual pipeline, no feature flag.
Work is sequenced across phases — the old engine is bypassed as the new path is
built, and deleted in Phase 4 — but the merged final state contains only the new
surfaces. Ariel holds no production data; migrations drop tables freely with no
data-migration steps.

This document supersedes the proactivity content of
[../north-star-cutover.md](../north-star-cutover.md): the separate "Proactive
case" runtime flow and the "proactive deliberation gets no tools" rule both
describe the pipeline this cutover deletes.

## Thesis

Proactivity is not a subsystem. It is the main agent loop, reachable by
non-human triggers, plus a durable scheduler.

Every wake reaches the same agent with the same faculties: memory, tools, and
judgment. The agent decides what the wake means and what to do about it,
including nothing. External events enter durable ingestion tasks first; one
durable queue holds future wakes, and the agent reaches it through one syscall.

There is no ambient pipeline, no case, no observation record, no deliberation
subagent, no proactive decision table. Code owns rails — the queue, ingress
normalization, provider auth, capability policy, approval, delivery. The model
owns every judgment: whether an event matters, whether to interrupt, what to do,
when to look again.

## What This Replaces

The survey found proactivity over-built and under-finished at once:

- `proactivity.py` (4,742 lines) is an ambient→case→deliberation pipeline, a
  Google-Workspace commitment tracker, and a feedback-learning subsystem welded
  together.
- The scheduler is implemented four times: `background_tasks` plus three bespoke
  timer state machines (`work_follow_up_loops`, `leave_by_reminders`,
  `email_thread_watches`).
- The proactive cluster is 18 tables.
- Push is unarmed — a verified webhook receiver with no registered `watch`. The
  periodic poll never runs. The agent cannot schedule anything.

The pipeline encodes a deterministic premise — that an ambient event is a
different kind of thing than a user message — that [../ai-first.md](../ai-first.md)
does not support. The crystallized design deletes the premise: one loop meets
every wake.

## Target Architecture

### One loop

A single agent-loop entrypoint runs every wake. It takes a wake-context — the
trigger and its data — assembles memory and eligibility facts, runs the answer
model with the `run` tool, executes the program, and emits any output. It is the
entrypoint that today serves a user message; the cutover makes it
trigger-agnostic.

A proactive wake is a normal turn. It receives the same `run` tool and the same
memory — the retriever and rememberer run as on any turn. It may end without
emitting; "do nothing" is an empty turn, not a represented decision.

### Triggers

Wake sources reach the agent through the worker:

- **A user message** — Discord or API. Already wired.
- **A provider push event** — a Gmail or Calendar `watch` callback. Ingress
  verifies, normalizes, and enqueues `provider_event_received`; the worker
  converts that to `provider_sync_due`, and only a sync that finds new data
  enqueues `agent_wake`.
- **An Agency job event** — ingress verifies, normalizes, and enqueues
  `agency_event_received`; the worker updates job state and enqueues
  `agent_wake` only for job states the agent should review.
- **A poll result** — a periodic provider sync finds new or changed data and
  enqueues a wake.
- **A due scheduled task** — a `background_tasks` row whose `run_after` has
  arrived.

There is no periodic sweep of internal tables for "ambient candidates."
External events enter durable ingestion tasks, and wakes are enqueued when the
worker has actionable data or status for the agent.

### One scheduler

`background_tasks` is the one durable task queue. It is shared infrastructure,
not proactivity-exclusive. The standing queue contract, row shape, task-type
ownership, and `agent_wake` payload shapes are owned by
[proactivity.md](proactivity.md), `BackgroundTaskRecord`, and `worker.py`.

The single-threaded worker takes the earliest due row, dispatches by
`task_type` (`agent_wake` invokes the loop; the surviving non-proactive types
keep their handlers), then deletes the row or re-arms it when
`recurrence_seconds` is set. A row is deleted only on success; a crash mid-wake
leaves it to retry. Effects that must not repeat carry an idempotency key in the
capability layer. P4 drops the proactivity-specific columns and the
claim/heartbeat/dead-letter/reaper machinery; at single-worker scale "a row
exists" is the only pending state needed.

Provider sync and `watch` renewal are recurring maintenance the worker performs
from connector state. They are plumbing, not agent wakes, and not a second
queue.

### The `proactive.schedule` syscall

The `run` program gets one new syscall: `proactive.schedule(when, note)`. It
writes a `background_tasks` row — `run_after` from `when`, `payload` from `note`.
`when` is a one-shot time; a recurring routine is the agent scheduling the next
wake from the current one. The run surface exposes no callable for listing,
inspecting, or canceling pending wakes. This is the agent's entire scheduling
surface — one entrypoint, by analogy to memory's `memory.recall` and
`memory.remember`.

A user reminder, a "check back on this later," a recurring routine — each is the
agent calling `proactive.schedule`.

### Autonomous action

The agent acts on its own initiative: a proactive wake may call any capability.
The gate on dangerous action is the per-capability policy. Every high-impact,
irreversible, or externally-visible capability is `requires_approval` — it routes
to a Discord approval the user confirms. Low-impact capabilities run inline.

There is no `autonomy_scopes` table. Autonomy is initiative, not
pre-authorization; the standing-grant layer is removed. Because a proactive wake
can run on tainted input — an email carrying a prompt injection — the
per-capability `requires_approval` policy is the security boundary. A
tainted-input wake that is fooled into proposing a harmful action produces an
approval prompt the user denies; it cannot act irreversibly on its own. Auditing
that policy is part of this cutover, not cleanup after it.

### Delivery

Delivery is one code path: post a message to the user's Discord.
`agent.emit_message` behaves the same whether the wake was a user message or an
email. There is no `notifications` table — Discord is the record of what was
sent, and every wake is recorded as a session turn.

### What dissolves

Commitment tracking, work follow-ups, leave-by reminders, and email
thread-watching are not refolded — they dissolve. Each becomes something the
agent does with the faculties it already has: calendar and email access, the
maps capability, `proactive.schedule`, and memory. "Follow up on this thread,"
"remind me to leave on time," and "track what I owe Bob" are agent behaviors,
not coded subsystems. The `cap.maps.directions` capability,
`proactive.schedule`, and the `provider_evidence` mail/calendar substrate
survive — they are the tools the behavior is built from.

## The Cutover

Five phases. Each is independently shippable and verified with ruff, ruff
format, mypy, and the full pytest suite; each migration runs up and down.
Phases 1-3 build the new path; the old engine is progressively bypassed and is
deleted in Phase 4, which lands only once the new path covers every wake source.

### P1 — The unified wake entrypoint

Extract the turn orchestration in `app.py` into one trigger-agnostic agent-loop
entrypoint that takes a wake-context. Route the existing user-message path
through it unchanged. Define the wake-context shape: the trigger kind and its
data. No proactive trigger is wired yet — this phase proves the entrypoint on
the trigger that already exists.

### P2 — The scheduler

Additive — the old engine keeps running. Lift `_wake` to a module-level
entrypoint and extract a `build_runtime` helper so the worker can construct the
runtime and invoke `_wake`. Add `recurrence_seconds` and the `agent_wake` task
type to `background_tasks`. Add a worker dispatch arm for `agent_wake` that
builds a wake-context from the row's `payload` and calls `_wake`; failed tasks
back off through `attempts`. Add the `proactive.schedule` syscall and its
`cap.proactive.schedule` capability. `recurrence_seconds` is for recurring
maintenance rows; agent recurrence is re-scheduling. After P2 a due `agent_wake`
task wakes the agent and the agent can schedule a future wake; the
proactivity-specific columns and the claim/heartbeat machinery still stand and
are removed in P4.

### P3 — Provider ingestion: push and poll

Register a Gmail `watch` (Cloud Pub/Sub topic and subscription) and Calendar
push channels when a Google connector connects; persist channel identity and
expiry on the connector. Extend the worker with connector maintenance: re-arm
each `watch` before expiry — Gmail daily, Calendar before its ~7-day limit — and
run a periodic provider sync as the reconcile baseline, independent of push.
Handle a stale cursor: a Gmail `404` or Calendar `410` clears cursor state and
triggers a full resync. A provider push event enqueues ingestion work; a sync
that finds new data enqueues an agent wake.

### P4 — The deletion sweep

With every wake-producing path routed through the unified entrypoint, delete the
old engine:

- Delete `proactivity.py`, `attention_ranking.py`, `workspace_reasoning.py`, and
  `leave_by.py` — roughly 5,700 lines — plus the proactive portions of
  `worker.py`, `sync_runtime.py`, and `app.py`. Any rail found load-bearing
  elsewhere moves to its owner.
- Drop 18 tables: the 8 `proactive_*` tables, `autonomy_scopes`, the 5 `work_*`
  tables, `leave_by_reminders`, `notifications`, `notification_deliveries`, and
  `email_thread_watches`.
- Reshape `background_tasks`: drop the proactivity-specific columns
  (`work_follow_up_loop_id/_version/_scheduled_for`) and the proactive task
  types from the CHECK enum; remove the claim protocol — `status`,
  `claimed_by`, `last_heartbeat`, `max_attempts`, `error`, the stale-task
  reaper, and the dead-letter state — leaving the shared durable queue shape
  documented in [proactivity.md](proactivity.md).
- Delete the proactive feedback endpoints, the `cap.email.thread_watch.*`
  capabilities, and the work-commitment API.
- Audit the capability registry: every high-impact, irreversible, or
  externally-visible capability is `requires_approval`.

### P5 — Docs

Write [proactivity.md](proactivity.md) — the standing module doc, modeled on
[memory.md](memory.md): the wake model, the triggers, the `background_tasks`
queue, the `proactive.schedule` syscall, delivery, and the rules. Rewrite the
proactivity sections of [../ai-first.md](../ai-first.md) and
[../north-star-cutover.md](../north-star-cutover.md) to the unified-loop model.
Keep leave-by behavior documented as emergent proactivity in
[maps-expansion-cutover.md](maps-expansion-cutover.md) and
[proactivity.md](proactivity.md). Reconcile `coordination.md` with the
simplified queue and relocate it out of `docs/modules/`. Fix the stale
endpoints in `production-runbook.md`. Update both doc indexes.

## Data Model End State

Proactivity adds no table of its own. Its scheduler is `background_tasks` — the
existing shared durable queue, simplified:

| Table | Role |
|---|---|
| `background_tasks` | The one durable task queue, shared across subsystems. Its row shape is owned by [proactivity.md](proactivity.md). |

Unchanged and not proactivity-owned: `provider_evidence` and
`provider_evidence_blocks` (the mail/calendar data substrate), `ai_judgments`
(memory's audit log — proactivity writes no rows to it), the session, turn, and
memory tables, the Agency `jobs` tables, and the `action_runtime` side-effect
and approval records.

Dropped (18): `proactive_observations`, `proactive_cases`,
`proactive_case_events`, `proactive_decisions`, `proactive_action_plans`,
`proactive_action_executions`, `proactive_feedback`, `proactive_learning_records`,
`autonomy_scopes`, `work_threads`, `work_commitments`, `work_commitment_sources`,
`work_follow_up_loops`, `work_follow_up_events`, `leave_by_reminders`,
`notifications`, `notification_deliveries`, `email_thread_watches`.

## Hard-Cutover Decisions

- One agent loop serves every wake; there is no separate proactive cognition
  path.
- `background_tasks` is the only queue, timer, and scheduler.
- The agent's scheduling surface is one syscall, `proactive.schedule`.
- Feedback is deleted; a proactive correction is a memory write.
- Delivery is one code path; there is no `notifications` table.
- `autonomy_scopes` is deleted; per-capability `requires_approval` is the
  autonomous-action boundary.
- Commitment tracking, work follow-ups, leave-by, and thread-watching are
  emergent agent behavior, not code.
- No compatibility layer, dual pipeline, or feature flag.

## Acceptance Criteria

- Every wake-producing worker path invokes one shared agent-loop entrypoint.
- A proactive wake has the same `run` tool and memory as a user turn and may end
  without emitting.
- `background_tasks` keeps one `task_type` discriminator and gains
  `recurrence_seconds`; its proactivity-specific columns and the
  claim/heartbeat/dead-letter machinery are gone.
- `proactivity.py`, `attention_ranking.py`, `workspace_reasoning.py`, and
  `leave_by.py` are deleted; the 18 tables are dropped.
- The `run` program has a `proactive.schedule` syscall; the agent can create
  future wakes.
- Gmail and Calendar `watch` are registered and renewed before expiry; a
  reconcile poll runs independent of push; a stale cursor triggers a full
  resync.
- Every high-impact, irreversible, or externally-visible capability is
  `requires_approval`; a tainted-input wake cannot act irreversibly without an
  approval.
- A proactive wake is recorded as a session turn; no proactivity-specific audit
  table exists.
- No `proactive_feedback` or `proactive_learning_records` exists; `ai_judgments`
  still exists and proactivity writes no rows to it.
- ruff, ruff format, mypy, and the full pytest suite pass; every migration runs
  up and down.

## Non-Goals

- No ambient pipeline, case, observation, decision record, or deliberation
  subagent.
- No triage tier or cheap-model pre-filter. If wake volume ever warrants it, the
  lever is deterministic coalescing of a burst into one wake — never a
  re-introduced pipeline.
- No `autonomy_scopes` or standing-grant system.
- No proactivity-specific audit, delivery, or feedback table.
- No second queue and no durable-execution engine.
- No compatibility mode for the deleted pipeline.

## Risks

- **Tainted-input action.** A proactive wake runs on untrusted content and could
  be prompt-injected. Mitigation: taint tracking, and per-capability
  `requires_approval` on every dangerous capability — the P4 registry audit is
  load-bearing, not cleanup.
- **Silent push death.** A Gmail or Calendar `watch` that is not renewed stops
  delivering with no error. Mitigation: renewal is first-class worker
  maintenance and its failure is surfaced; the reconcile poll is the backstop.
- **The deletion sweep is large.** P4 removes four modules and 18 tables.
  Mitigation: P4 lands only after P1-P3 verifiably handle every wake source.
- **In-flight leave-by.** `leave_by.py` and `leave_by_reminders` are recent,
  partly uncommitted work. P4 deletes them; `cap.maps.directions` survives.
  Sequence P4 so leave-by is not extended and deleted at once.
- **Wake cost.** Every wake spends a model call. Acceptable at single-user
  volume with prompt caching; the lever if it bites is coalescing, a rail.

## Source Findings

This plan is based on the May 2026 proactivity SME survey — seven parallel
sub-agents over `proactivity.py`, the inbound surface, the worker and scheduler,
the data model, the capability surface, the docs, and external grounding — a
focused deep-dive on the commitment subsystem, and the architecture review that
replaced the ambient-pipeline design with the unified-loop model.
