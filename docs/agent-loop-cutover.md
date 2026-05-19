# Agent Loop Cutover

## Scope

This document is the hard-cutover plan for Ariel's agent loop. It makes turns
asynchronous and durable, makes the loop long and adaptive, fixes the
cross-program context leak, wires worker-run delivery, and adds a read-only
research subagent.

It owns the cutover only. The standing design lands in
[modules/agent-loop.md](modules/agent-loop.md), written by P4. The plan inherits
[ai-first.md](ai-first.md), [cleanliness.md](cleanliness.md), and
[simplicity.md](simplicity.md), and follows the precedent of the proactivity
crystallization ([modules/proactivity-cutover.md](modules/proactivity-cutover.md)):
keep one model loop and a thin rail; delete everything that is not that.

It narrows [run-program-cutover.md](run-program-cutover.md) and
[modules/proactivity.md](modules/proactivity.md): the synchronous request/response
turn and the 2-attempt model-call cap are removed. Where this document conflicts
with turn-lifecycle language in those docs, this document wins; P4 reconciles
them.

The cutover is hard. There is no compatibility layer, no dual turn path, no
feature flag, and no fallback. Work is sequenced across phases — P0 and P1 stand
up the new path, the synchronous path is deleted in P1 — but the merged final
state runs every turn through the worker. Ariel holds no production data;
migrations alter the `background_tasks` and `turns` tables freely with no
data-migration step.

## Thesis

> **Reconciliation note (P4).** The plan below proposed extracting the loop body
> into a single shared `run_agent_loop` function. The implementation did not do
> that. The two agents are **two separate reason→act→observe loops** — `_wake`
> in `app.py` and `run_research` in `research_runtime.py` — that share the
> run-program model and `execute_run_program` (one program's sandboxed
> execution and the syscall rails), not an outer-loop function. Every claim of
> a shared `run_agent_loop` below is superseded; the standing design is
> [modules/agent-loop.md](modules/agent-loop.md).

There is one run-program loop pattern. It runs as two agents — the **main
agent** and a **research subagent** — reached through `_wake` and the
`background_tasks` queue.

The main agent owns the user-facing conversation and every write. It runs a
long, adaptive reason→act→observe loop, single-threaded, and it owns coupled
work end to end. The research subagent runs the same loop pattern with a
read-only capability whitelist and a structured-finding output; the main agent
dispatches it for breadth-first, read-heavy, independent investigation, and its
context is discarded once it returns its finding.

This is delegation without an orchestration layer. There is no router, no
management agent, no triage tier. The main agent — the model — decides to
delegate by calling a syscall; deterministic code never routes between agents.

## What This Replaces

The agent-loop survey (May 2026) and the SOTA verification that followed found
four faults:

- **The loop is too short.** `max_model_attempts` is 2 and `max_turn_wall_time_ms`
  is 20s (`config.py`). The agent gets two model calls per turn — it cannot
  observe, reason, act, and observe again across a real task.
- **A long turn cannot be synchronous.** A user-message turn is a synchronous
  FastAPI handler (`post_message` in `app.py`); the Discord client blocks on it
  with a hard 60s timeout (`discord_bot.py`). The whole turn holds one
  `SERIALIZABLE` transaction and a session advisory lock for its full duration.
- **`emit_value` leaks into context.** Each `run` program is a fresh sandbox
  process with no shared state, so `agent.emit_value` is the only way to carry
  data between a turn's programs — and the emitted values accumulate untrimmed
  in the model's context (`responses_input_items` is built once per turn and
  never trimmed). A long loop would grow context without bound.
- **Worker-run turns cannot reach the user.** `agent.emit_message` from a
  worker-run turn writes `TurnRecord.assistant_message` and stops. Proactive
  delivery was specified in `modules/proactivity.md` and never built;
  `discord_notification_timeout_seconds` is its declared-but-unused remnant. A
  proactive wake today runs, emits, and reaches no one.

These are replacement targets, not compatibility promises.

## Goals

- Make every turn an asynchronous, worker-run unit of work reached through the
  `background_tasks` queue; the HTTP ingress enqueues and returns immediately.
- Wire worker-run delivery so a turn's emitted message reaches the user — for
  user turns, proactive wakes, and research completions alike.
- Make the loop long and adaptive: many reason→act→observe rounds, bounded by a
  wall-clock budget the model can see, with stuck-detection and a graceful
  exhaustion path.
- Keep large data out of the model's context with a host-side per-turn scratch
  store, extending the context firewall across a turn's programs.
- Add one read-only research subagent — the same loop in a restricted
  configuration — dispatched by a `research.investigate` syscall, returning a
  typed, tainted finding.
- Keep the model's tool surface exactly `run`; keep every capability rail
  (policy, taint, approval, egress, audit) at the syscall boundary, unchanged.
- Preserve `_wake` as the single trigger entrypoint and `background_tasks` as
  the single queue.

## Non-Goals

- No orchestration layer, router, management agent, triage tier, or pre-filter.
  The agent delegates by calling a syscall; deterministic code never routes.
- No second model-facing tool. The tool surface stays exactly `run`.
  `research.investigate` is a syscall, like `agency.run`.
- No second cognition path and no second trigger entrypoint. `_wake` serves
  every trigger; the research loop is a worker-task handler, not a trigger.
- No recursive delegation. The research subagent cannot call
  `research.investigate`; it is single-level.
- No research run that combines private-data reads with open-web reach. The two
  research modes are mutually exclusive per run.
- No free-text-only finding. A finding is a typed, structured object carried
  with tainted provenance.
- No durable syscall journaling or program replay. A program runs once; a
  crashed turn is retried as a fresh wake, not replayed (unchanged from
  `run-program-cutover.md`).
- No new queue, scheduler, or durable-execution engine. `background_tasks`
  carries the new task types.
- No new table for research runs; they reuse `TurnRecord`.
- No interim-progress messaging, no parallel research fan-out, no review
  subagent. These are follow-ons, named below, deliberately out of scope.
- No compatibility mode for the synchronous turn path.

## Target Architecture

### One loop pattern, two agents

> **Reconciliation note (P4).** No shared `run_agent_loop` function was
> extracted. `_wake` and `run_research` are two separate loops; they share the
> run-program model, `execute_run_program`, and the `run_runtime.py` helpers —
> the per-program executor, not an outer-loop function. The two "configurations"
> below are the real differences between the two loops.

The loop is the existing `run`-program loop: the model authors a `run` program,
`execute_run_program` runs it in the gVisor sandbox, syscalls round-trip through
the host rails, the loop feeds results back and calls the model again.

The two agents differ in: the eligible capability whitelist, the wall-clock
budget, the model-call backstop, the output mode, and the system prompt.

- **Main agent** — every eligible capability (writes gated by
  `requires_approval`); output mode `message` (`agent.emit_message` ends the
  loop and is delivered to the user); the conversational system prompt. The
  `_wake` loop in `app.py`.
- **Research subagent** — a read-only capability whitelist; output mode
  `finding` (a typed `research_finding_v1` object ends the loop); the research
  system prompt. The `run_research` loop in `research_runtime.py`.

The run-program executor is one. `_wake` and `run_research` are sibling loops
around it, each owning its own outer mechanics.

### Async turns and delivery

Every turn runs in the single-threaded background worker. The HTTP ingress —
the user-message endpoint (`post_message`) — enqueues a `background_tasks` row
and returns `202`; it never runs a turn. The worker takes the earliest due row,
dispatches by `task_type`, runs the turn, and on success deletes the row.

A turn's emitted message is delivered by the worker after the turn commits: it
posts the message to the user's Discord channel over the Discord REST API
(`discord_channel_id`, `discord_bot_token`, `discord_notification_timeout_seconds`).
This is one delivery path for every turn — a user reply, a proactive wake, a
research result.

Because every turn runs in the one single-threaded worker, turns are serialized
by construction. The per-turn session advisory lock is removed: it guarded
against concurrent turns on one session, which can no longer occur.

### Durability — per-program commit

A turn commits once per `run` program. A program that completes cleanly
(`RunProgramResult.program_ok`) has its effects committed; a failed program
commits its syscall-trace audit (events and action attempts) with its staged
approvals voided — it is not rolled back. There is no turn-spanning
transaction; each transaction is one program long, seconds not minutes.

A turn is not journaled and not replayed. If the worker crashes mid-turn, the
`background_tasks` row was not deleted and is retried as a fresh wake — the model
re-evaluates from current committed state. Committed programs' effects stand;
write capabilities carry capability-layer idempotency keys, so a re-run cannot
double-send. This is the documented single-user tradeoff and is consistent with
`run-program-cutover.md` ("a program runs once within a turn").

### The long adaptive loop

`max_model_attempts` and `max_turn_wall_time_ms` are removed. The loop runs
reason→act→observe rounds until the model emits its terminal output (a message,
for the main agent; a finding, for research) or its wall-clock budget is spent.
On budget exhaustion the loop ends gracefully — the main agent emits a "ran out
of time, here is where I got to" message; a research run returns a `partial`
finding.

Three rails bound the loop:

- **Wall-clock budget** — `main_turn_budget_seconds` and
  `research_run_budget_seconds`, per configuration. Generous but moderate: agent
  reliability degrades super-linearly with turn length.
- **Model-call backstop** — `agent_loop_max_model_calls`, a high paranoid cap;
  not the primary control.
- **Stuck-detection** — the loop halts if a program repeats an identical
  capability syscall (same `capability_id` and input) past a small threshold, or
  if no program across several consecutive rounds emits a value, emits a
  message, or makes a state-changing syscall. A halted loop ends gracefully.

The model is told its remaining budget each round, as a short system line, so it
paces itself rather than running optimistically into the limit.

### The scratch store

A turn gets a host-side, per-turn **scratch store**: a `dict` threaded through
the loop and into `execute_run_program` the way `runtime_provenance` is
threaded. Two syscalls reach it, handled inline in
`run_runtime.py`'s `syscall_callback` beside the `agent.*` syscalls — they are
not capabilities and not routed through `process_one_call`:

- `scratch.set(key, value)` — stores `value` host-side under `key`.
- `scratch.get(key)` — returns the stored value into the program.

Each stored value carries the taint of the program that set it; `scratch.get`
re-applies that taint, so untrusted data carried across programs stays tainted.
The store is bounded host-side (max entries, max bytes per entry, max total) and
is cleared at end of turn.

Large intermediate data — search results, fetched pages, mailbox extracts —
lives in the scratch store as host-side values. Only keys, and the summaries the
model deliberately surfaces with `agent.emit_value`, enter the model's context.
`agent.emit_value` keeps its role — the model's deliberate channel for data it
wants to reason over next round — but it is no longer the *only* way to carry
data forward, so it no longer has to. Old `emit_value` rounds are evicted from
`responses_input_items` as a backstop: only the most recent round is retained.

The system-prompt prefix is held byte-stable across rounds (no per-round
timestamps in the prefix) to preserve the model provider's prompt cache.

### The research subagent

The main agent dispatches research with the `research.investigate(question, mode)`
syscall. The syscall enqueues a `research_run` task on `background_tasks` and
returns a handle immediately; the main agent acknowledges to the user ("looking
into that") and ends its turn. The main agent owns clarification — it resolves an
ambiguous request with the user *before* dispatching.

The worker runs the `research_run` task: `run_research` runs the read-only
research loop. The run opens with an explicit **plan step** — the model
writes its sub-questions before searching — then runs the read-only loop on the
mode's whitelist, using the scratch store to hold raw evidence. It terminates on
a typed `research_finding_v1`. The research run is recorded as a `TurnRecord`
with `kind = research`; no new table.

On completion the worker enqueues an `agent_wake` carrying the finding. The main
agent wakes, the finding enters its context with **tainted provenance** — the
main agent treats it as untrusted content, exactly like a fetched web page — and
the main agent answers the user. Because the finding is tainted, any action it
motivates is evaluated tainted and routes through `requires_approval`.

### The two research modes

A research run is in exactly one of two mutually exclusive modes. This is the
"Rule of Two": a run is exposed to at most two of {untrusted input, private
data, outbound reach}, never all three — the precondition for silent data
exfiltration.

- **`web`** — whitelist `cap.search.web`, `cap.search.news`, `cap.web.extract`.
  Untrusted input and outbound reach; no private data. Web fetches go through
  those capabilities' existing host-safety check and egress controls, and each
  fetch is a syscall recorded in the `action_attempts` audit ledger.
- **`personal`** — whitelist `cap.email.search`, `cap.email.read`,
  `cap.drive.search`, `cap.drive.read`, `cap.calendar.list`. Private data and
  untrusted input (the user's mailbox is attacker-influenced); no outbound web
  reach.

A run never holds both. A task that needs both web and personal evidence is two
runs; the main agent combines their findings — coupled synthesis stays with the
single main thread.

## Target Behaviour

### A user turn

1. The user sends a Discord message. The Discord bot forwards it to the
   user-message HTTP endpoint and does not wait for a reply.
2. The endpoint enqueues a `user_message` task on `background_tasks` — payload:
   the message text, the Discord channel and message identity, ingress
   provenance — and returns `202`.
3. The worker takes the task and calls `_wake` with a `WakeContext`
   (`trigger_kind = user_message`).
4. `_wake` assembles memory and context and runs the main-agent loop. The loop
   runs as many rounds as the task needs, within `main_turn_budget_seconds`,
   committing per program.
5. The loop ends when the model calls `agent.emit_message`.
6. The worker posts the message to the user's Discord channel, deletes the task
   row, and enqueues the memory rememberer as today.

### A long adaptive turn

The user asks for coupled multi-step work. The loop runs many rounds: the model
fetches with capability syscalls, stashes raw results with `scratch.set`,
reasons over `scratch.get` values and small `emit_value` summaries, and acts —
each write capability staging a `requires_approval` proposal. The model sees its
remaining budget each round. The turn commits after each program. It ends with a
single `agent.emit_message`; truly long-running deep investigation is delegated
to a research run instead.

### A research run

1. Inside a turn, the main agent calls `research.investigate(question="...",
   mode="web")`. The syscall enqueues a `research_run` task and returns
   `{status: "queued", research_id: "..."}`.
2. The main agent emits a message acknowledging the request and ends its turn.
3. The worker takes the `research_run` task and calls `run_research`.
4. `run_research` runs the research loop in `web` mode: a plan step, then a
   read-only loop of `search.web` / `web.extract` calls, raw evidence held in
   the scratch store, within `research_run_budget_seconds`.
5. The loop terminates on a `research_finding_v1`. The worker records the run as
   a `research` `TurnRecord` and enqueues an `agent_wake` carrying the finding.
6. The worker runs the `agent_wake` turn: the finding enters context tainted,
   the main agent reads it and emits a message; the worker delivers it.

### A proactive wake

A provider push, a poll result, or a due scheduled task enqueues an `agent_wake`,
exactly as `modules/proactivity.md` describes. The worker runs it through
`_wake`; the loop runs; the wake may end without emitting. If it emits, the
worker delivers the message to Discord — the delivery path P0 builds. Proactive
delivery, broken today, works.

## Composition With Existing Systems

- **Proactivity.** `_wake` remains the one trigger entrypoint for every trigger
  — user message, provider push, poll, scheduled task, and now research
  completion. A research completion is a wake trigger of the same class as an
  Agency job completion. P0 repairs proactive delivery as a side effect. The
  research loop is not a trigger and not a `_wake`; it is a worker-task handler,
  so the "one entrypoint" rule holds.
- **The `run` program and sandbox.** Unchanged. Both loop configurations author
  `run` programs and execute them through `execute_run_program` in the existing
  persistent gVisor sandbox. The research config differs only by the
  `allowed_capability_ids` set passed in — already a first-class parameter of
  `execute_run_program`.
- **The capability rails.** Unchanged. Every research-config syscall is routed
  through `process_one_call` — policy, taint, provenance, egress, output
  guardrails, the `action_attempts` ledger — exactly as a main-agent syscall.
  The research whitelist contains only `impact_level = read` capabilities, so no
  research syscall stages an approval.
- **Memory.** The retriever and rememberer run in `_wake` (main-agent config) as
  today. The research subagent does not use them — it works on a bounded
  question, not a conversation. When the main agent answers from a finding, the
  rememberer captures whatever is worth keeping, as on any turn. The scratch
  store is per-turn and ephemeral; it is not memory.
- **Agency.** `agency.run` is the precedent for `research.investigate`: a syscall
  that dispatches async work and whose completion returns as an `agent_wake`.
  They coexist — Agency owns code execution, research owns read-only
  investigation. `research.investigate` is `allow_inline` where `agency.run` is
  `requires_approval`, because a research run is strictly read-only.
- **`background_tasks`.** Gains two `task_type` values, `user_message` and
  `research_run`. No new queue, no new table. The worker gains a dispatch arm
  for each.
- **`action_attempts` and approvals.** A research run's read syscalls write
  `ActionAttemptRecord` rows — the syscall trace, the audit spine — like any
  turn. A research run stages no approvals.

## Capability Contract

### `cap.research.investigate`

A new `CapabilityDefinition` in `capability_registry.py`:

- `capability_id` — `cap.research.investigate`
- `version` — `v1`
- `impact_level` — `read`
- `policy_decision` — `allow_inline`
- `contract_metadata` — `input_schema: research_investigate_v1`,
  `output_schema: research_task_start_v1`, `idempotency: action_attempt_id`,
  `execution_mode: background_task_enqueue`
- `allowed_egress_destinations` — `()` — the syscall itself reaches nothing; the
  research *run* reaches the network only through its mode whitelist's own
  capabilities, each with its own contract
- `validate_input` — normalizes and checks `research_investigate_v1`
- `execute` — enqueues a `research_run` `background_tasks` row and returns
  `research_task_start_v1`
- run-callable alias — `research.investigate` (in `_RUN_CALLABLE_ALIASES`)

`research_investigate_v1` (input): `question: str` (required, non-empty),
`mode: "web" | "personal"` (required).

`research_task_start_v1` (output): `status: "queued"`, `research_id: str`.

The two mode whitelists are module-level frozensets in `capability_registry.py`,
beside the existing `*_CAPABILITY_IDS` constants:
`RESEARCH_WEB_CAPABILITY_IDS = {cap.search.web, cap.search.news, cap.web.extract}`
and `RESEARCH_PERSONAL_CAPABILITY_IDS = {cap.email.search, cap.email.read,
cap.drive.search, cap.drive.read, cap.calendar.list}`.

### The scratch syscalls

`scratch.set` and `scratch.get` are host-side inline syscalls, not capabilities.
They are added to the always-eligible syscall set in `run_runtime.py` beside
`_AGENT_SYSCALL_NAMES` and handled in `syscall_callback`:

- `scratch.set(key: str, value: <json>)` — stores `value`; returns `None`.
  Errors: `scratch_key_invalid`, `scratch_value_too_large`, `scratch_store_full`.
- `scratch.get(key: str)` — returns the stored value, with the setting program's
  taint re-applied. Error: `scratch_key_missing`.

## API Design

### HTTP

- The user-message endpoint stops running turns synchronously. It validates its
  input, enqueues a `background_tasks` row, and returns `202` with
  `{status: "accepted", task_id: "..."}`. Idempotency guards the enqueue; no
  response payload is cached, because there is no synchronous response.
  `/v1/captures/record` stays synchronous and does not enqueue a wake.
  `/v1/captures` is deleted.
- A turn's result is observed through the existing session-events endpoint, or —
  for Discord, the primary surface — delivered as a pushed Discord message.

### Syscalls

- `research.investigate(question, mode)` — new; see the capability contract.
- `scratch.set(key, value)` / `scratch.get(key)` — new; see above.
- `agent.emit_message`, `agent.emit_value`, `agent.pause_until_input` —
  unchanged in signature. `agent.emit_message` remains the loop's terminal
  output for the main-agent config.
- The `run` tool definition is unchanged: one tool, `run`, source a Python
  program.

### `WakeContext`

`WakeContext` keeps `trigger_kind`, `prompt_text`, `discord_context`,
`attachment_sources`, `ingress_provenance`. After P1 a `user_message` wake is
built from the queued task payload rather than from a live request; its
`discord_context` carries the channel and message identity the worker needs to
deliver the reply.

### `research_finding_v1`

The research loop's terminal output and the payload the completion `agent_wake`
carries:

- `question: str` — the question investigated
- `mode: "web" | "personal"`
- `status: "complete" | "partial" | "failed"` — `complete` when the run called
  `research.finding`; `partial` when the budget, the model-call backstop, or
  stuck-detection ended the run first; `failed` when a model call raised
- `summary: str` — a bounded synthesis (host-enforced max length)
- `claims: list` — model-shaped as `{ statement, sources, confidence }`
- `gaps: list` — what could not be determined
- `sources: list` — model-shaped as `{ title, reference, retrieved_at }`

The host validates `claims`, `gaps`, and `sources` only as lists (under a
total-size bound); their inner element shapes are specified to the research
model in its prompt, not hard-validated host-side — deliberately: hard inner
validation is brittle, and the containment is taint, not structure.

The finding is typed and bounded, but its text fields are model-authored over
untrusted content; it is therefore carried and rendered with tainted provenance.
Containment is the taint plus `requires_approval` on every action — not the
absence of prose.

## Key Decisions

- **Async, worker-run turns.** A long loop cannot be synchronous: the Discord
  client times out at 60s and a synchronous turn holds a `SERIALIZABLE`
  transaction and a session lock for its full duration. Every turn runs in the
  worker; the HTTP ingress enqueues and returns. This also completes the
  unified-loop model — every trigger now reaches `_wake` through the one queue.
- **Per-program commit, no journaling.** Committing per `run` program bounds
  each transaction to seconds and bounds crash loss to one program. A crashed
  turn is retried as a fresh wake, not replayed; idempotency keys on write
  capabilities make the retry safe. Consistent with `run-program-cutover.md`'s
  "a program runs once".
- **The advisory lock is removed.** The single-threaded worker serializes turns
  by construction; a per-turn lock is redundant.
- **One loop, two configs — not an orchestration layer.** The research subagent
  is the same loop with a different whitelist, budget, output mode, and prompt.
  There is no router and no manager. The main agent delegates by calling a
  syscall. This is the 2026 SOTA consensus and the repo's own subagent rule.
- **Two mutually-exclusive research modes.** A subagent reading both private
  data and the open web in one run is the lethal trifecta; "read-only" does not
  close the exfiltration channel of its own web fetches. The mode split is the
  Rule of Two; it preserves both capabilities while closing the trifecta.
- **The finding is typed and tainted.** A typed structure bounds the finding; a
  tainted provenance makes the main agent treat it as untrusted, so a
  prompt-injected finding cannot authorize an unapproved action.
- **`research.investigate` is `allow_inline`.** A research run causes no side
  effect — it is strictly read-only — so dispatching it needs no approval. This
  is the deliberate contrast with `agency.run`, which writes code and is
  `requires_approval`.
- **The scratch store, not bigger `emit_value` limits.** A host-side per-turn
  key/value store carries large data between programs without it entering the
  model's context — the "memory pointer" pattern. It extends the context
  firewall across a turn; `emit_value` stays only for what the model
  deliberately wants in context.
- **Budget the model can see; stuck-detection it cannot.** Reliability degrades
  with turn length, so the budget is moderate and visible to the model for
  pacing; runaway behavior is caught deterministically, host-side, by
  stuck-detection the model cannot reason around.
- **Reuse `TurnRecord` for research runs.** A `kind` discriminator on `turns`
  avoids a new table — consistent with the schema-consolidation direction.

## The Cutover

Five phases. Each is independently shippable and verified with ruff, ruff
format, mypy, and the full pytest suite; each migration runs up and down. P0 and
P1 stand up the new path; the synchronous turn path is deleted in P1. The merged
final state runs every turn through the worker.

### P0 — Worker-run delivery

Wire `agent.emit_message` from a worker-run turn to the user. After the worker
runs a turn and it commits, the worker posts the turn's emitted message to
`discord_channel_id` over the Discord REST API, with `discord_bot_token` for auth
and `discord_notification_timeout_seconds` as the timeout. A wake that carries an
originating Discord message posts as a reply to it; a wake without one posts to
the default channel.

This phase stands alone and is independently valuable: it repairs proactive
delivery, which is broken today.

- **Files:** `worker.py` (deliver after each worker turn); a small Discord REST
  delivery helper; `config.py` (no new settings — the three exist).
- **Acceptance:** a worker-run `agent_wake` turn that emits a message delivers it
  to the Discord channel; a proactive wake reaches the user; `make verify`
  passes.

### P1 — Async turns

Make every turn worker-run and delete the synchronous path.

- The user-message endpoint enqueues a `user_message` `background_tasks` row
  and returns `202`. It no longer calls `_wake`. `/v1/captures/record` stays
  synchronous; `/v1/captures` is deleted.
- The worker gains a `user_message` dispatch arm: it builds a `WakeContext`
  (`trigger_kind = user_message`) from the row payload and calls `_wake`.
- The Discord bot forwards a message and does not block on a reply; the reply
  arrives via P0. Its synchronous reply path is deleted.
- The turn commits per `run` program: a clean program's effects commit; a
  failed program is not rolled back — its syscall-trace audit commits, its
  staged approvals are voided, its emitted outputs scrubbed (see Durability —
  per-program commit). No turn-spanning transaction.
- The per-turn session advisory lock is removed.

- **Files:** `app.py` (the two endpoints; remove the synchronous `_wake` call
  and the advisory lock; per-program commit in the `_wake` loop), `worker.py`
  (the `user_message` arm), `discord_bot.py` (fire-and-forget submit; delete the
  blocking reply path), `persistence.py` (the `user_message` task type),
  `alembic/versions/` (the `task_type` CHECK enum gains `user_message`).
- **Acceptance:** a user message returns `202` and is run by the worker; the
  reply is delivered via P0; no code path runs a turn synchronously; the
  advisory lock is gone; `make verify` passes; the migration runs up and down.

### P2 — Long adaptive loop and the scratch store

Make the loop long.

> **Reconciliation note (P4).** The `run_agent_loop` extraction below was not
> done. The long adaptive loop lives in `_wake` in `app.py`; P3 added a
> structurally mirrored sibling loop in `run_research`. The remaining P2 work —
> the budget, stuck-detection, the budget signal, the scratch store,
> `emit_value` eviction, prompt-prefix stability — all landed.

- Make the `_wake` loop long: it runs as many reason→act→observe rounds as the
  task needs (no shared `run_agent_loop` function was extracted).
- Remove `max_model_attempts` and `max_turn_wall_time_ms`. Add
  `main_turn_budget_seconds`, `research_run_budget_seconds`, and
  `agent_loop_max_model_calls` to `config.py`, validated and documented in
  `.env.example`.
- Add stuck-detection and the remaining-budget context line.
- Add the `scratch.set` / `scratch.get` syscalls and the host-side per-turn
  scratch store, threaded through the loop, taint-carrying, bounded, cleared at
  end of turn.
- Evict superseded `emit_value` rounds from `responses_input_items`; hold the
  system-prompt prefix byte-stable.

- **Files:** `app.py` (the long `_wake` loop, budget, stuck-detection,
  budget signal, scratch-store lifecycle, prompt-prefix stability),
  `run_runtime.py` (the `scratch.*` syscalls and store; `emit_value` eviction),
  `config.py` and `.env.example` (the budget settings).
- **Acceptance:** a turn runs many adaptive rounds bounded by the wall-clock
  budget; large data moved through `scratch.*` does not enter model context; the
  loop halts on stuck-detection and on budget exhaustion with a graceful
  message; `make verify` passes.

### P3 — The research subagent

Add the research subagent and its dispatch.

- Add `cap.research.investigate`, its validator, its `execute` (enqueue a
  `research_run` task), its run-callable alias, and the two mode-whitelist
  constants to `capability_registry.py`.
- Add `run_research` in `research_runtime.py`: the research loop — a sibling of
  the `_wake` loop, structurally mirrored — driven on the mode whitelist,
  bounded by `research_run_budget_seconds`, with the research prompt and the
  plan step, ending on `research.finding`.
- The worker gains a `research_run` dispatch arm: it calls `run_research`,
  records the run as a `kind = research` `TurnRecord`, and enqueues an
  `agent_wake` carrying the `research_finding_v1`.
- `web`-mode fetches go through the existing `cap.web.extract` / `cap.search.*`
  capabilities — they carry the existing host-safety check
  (`_is_unsafe_web_extract_host`) and egress controls, and every fetch is a
  syscall recorded in the `action_attempts` ledger. No separate browsing
  service or separate fetch log is added.
- The completion `agent_wake` renders the finding into the main agent's context
  as a tainted block.

- **Files:** `capability_registry.py` (the capability, alias, whitelists),
  `app.py` or a new `research_runtime.py` (`run_research`), `worker.py` (the
  `research_run` arm and the completion `agent_wake`), `action_runtime.py`
  (the `cap.research.investigate` execute path), `persistence.py` (the
  `research_run` task type; the `turns.kind` column), `alembic/versions/` (the
  `task_type` enum gains `research_run`; `turns` gains `kind`).
- **Acceptance:** the main agent dispatches `research.investigate` and gets a
  handle; the worker runs the research loop read-only in the requested mode; a
  `web` run cannot reach private-data capabilities and a `personal` run cannot
  reach the web; the finding returns via an `agent_wake` with tainted
  provenance; `make verify` passes; the migrations run up and down.

### P4 — Doc reconciliation

Write [modules/agent-loop.md](modules/agent-loop.md), the standing doc: the loop,
the two configurations, async turns, the scratch store, the research subagent,
delivery, and the rules. Reconcile `modules/proactivity.md` (delivery is wired;
the trigger list gains research completion), `ai-first.md` (the research
subagent joins the retriever and rememberer as a named subagent),
`run-program-cutover.md` and `north-star-cutover.md` (the synchronous turn and
the attempt cap are gone — note that `run-program-cutover.md`'s "proactive
deliberation receives no `run` program" line was already superseded by the
proactivity cutover), `coordination.md` (the new task types), the README, and
both doc indexes.

- **Acceptance:** the standing doc exists; no doc still describes a synchronous
  turn or a 2-attempt cap; the indexes are current; `make verify` passes.

## Files

Touched by the cutover, by module:

- `app.py` — endpoints become enqueue-only; advisory lock removed; the long
  `_wake` loop — budget, stuck-detection, budget signal, scratch-store
  lifecycle, prompt-prefix stability; per-program commit.
- `worker.py` — worker-run Discord delivery; `user_message` and `research_run`
  dispatch arms; the research-completion `agent_wake`.
- `run_runtime.py` — the `scratch.set` / `scratch.get` syscalls and store;
  `emit_value` eviction.
- `discord_bot.py` — fire-and-forget submit; the blocking reply path deleted.
- `capability_registry.py` — `cap.research.investigate`, its run-callable alias,
  the two mode-whitelist constants.
- `action_runtime.py` — the `cap.research.investigate` execute path.
- `persistence.py` — the `user_message` and `research_run` task types; the
  `turns.kind` column.
- `config.py`, `.env.example` — `main_turn_budget_seconds`,
  `research_run_budget_seconds`, `agent_loop_max_model_calls`;
  `max_model_attempts` and `max_turn_wall_time_ms` removed.
- `research_runtime.py` — new module: `run_research`, the research loop, and
  `ResearchFinding`. It does not import `app.py` (the worker imports both).
- `alembic/versions/` — the `background_tasks.task_type` CHECK enum;
  `turns.kind`.
- `docs/` — P4.
- `tests/unit/`, `tests/integration/` — per phase.

## Data Model And Configuration

No new table. `background_tasks` and `turns` change shape:

| Table | Change |
|---|---|
| `background_tasks` | The `task_type` CHECK enum gains `user_message` (P1) and `research_run` (P3). No column change; no new queue. |
| `turns` | Gains a `kind` column — `agent_turn` or `research` — so a research run reuses `TurnRecord` (P3). |

Configuration (`config.py`, `ARIEL_` env prefix, documented in `.env.example`):

- Removed: `max_model_attempts`, `max_turn_wall_time_ms`.
- Added: `main_turn_budget_seconds` (float), `research_run_budget_seconds`
  (float), `agent_loop_max_model_calls` (int, the backstop). Each validated
  positive, matching the `config.py` validator convention.
- Reused, no change: `discord_channel_id`, `discord_bot_token`,
  `discord_notification_timeout_seconds`, `worker_poll_seconds`.

## Rules

- There are two agent loops — `_wake` and `run_research` — each its own
  reason→act→observe loop, sharing `execute_run_program` and the run-program
  model. There is no third loop and no shared `run_agent_loop` function.
- `_wake` is the one trigger entrypoint. The research loop is a worker-task
  handler, not a trigger.
- The model's tool surface is exactly `run`. Delegation is a syscall, never a
  tool and never a deterministic route.
- Every turn runs in the single-threaded worker. The HTTP ingress enqueues and
  returns; it never runs a turn.
- A turn commits per `run` program. There is no turn-spanning transaction and no
  program journaling or replay.
- Every delivered message goes through the one worker-side Discord delivery
  path.
- The research subagent is strictly read-only, single-level, and runs in exactly
  one mode per run; a run never holds both private-data reads and open-web
  reach.
- A research finding is typed and is carried with tainted provenance.
- Large per-turn data lives in the scratch store; only keys and deliberate
  `emit_value` summaries enter the model's context.
- Every capability syscall — main agent or research — passes the unchanged
  `process_one_call` rails.

## Hard-Cutover Decisions

- The synchronous request/response turn is deleted, not flagged. The HTTP
  ingress only enqueues.
- The Discord bot's blocking reply path is deleted; replies are pushed.
- The per-turn session advisory lock is removed.
- `max_model_attempts` and `max_turn_wall_time_ms` are removed; the loop is
  bounded by a wall-clock budget and a model-call backstop.
- No compatibility layer, no dual turn path, no feature flag, no fallback.
- No durable journaling or replay; a crashed turn is retried as a fresh wake.

## Acceptance Criteria

The cutover is complete only when all are true:

- Every turn — user message, proactive wake, research completion — runs in the
  single-threaded worker, reached through `background_tasks`.
- The user-message endpoint enqueues a task and returns `202`; no code path
  runs a turn synchronously.
- A worker-run turn's emitted message is delivered to the user's Discord
  channel; a proactive wake reaches the user.
- The loop runs unbounded model rounds, bounded only by the wall-clock budget
  and the model-call backstop; `max_model_attempts` and `max_turn_wall_time_ms`
  do not exist.
- The loop halts deterministically on stuck-detection and ends gracefully on
  budget exhaustion; the model is told its remaining budget.
- A turn commits per `run` program; no turn-spanning transaction exists; the
  per-turn advisory lock is gone.
- `scratch.set` / `scratch.get` exist; large data moved through them does not
  enter model context; the scratch store is per-turn, taint-carrying, and
  cleared at end of turn.
- `cap.research.investigate` exists, is `allow_inline` and `read`, and dispatches
  a `research_run` task.
- A research run executes the `run_research` loop, read-only, in one mode; a
  `web` run cannot reach private-data capabilities and a `personal` run cannot
  reach the web.
- A research run terminates on a typed `research_finding_v1`; the completion
  `agent_wake` carries it; it enters the main agent's context with tainted
  provenance.
- The research subagent cannot call `research.investigate`.
- `background_tasks` has two new task types and no new column; `turns` has a
  `kind` column; no new table exists.
- The standing doc `modules/agent-loop.md` exists and the reconciled docs carry
  no synchronous-turn or attempt-cap language.
- ruff, ruff format, mypy, and the full pytest suite pass; every migration runs
  up and down.

## Risks

- **Retry of a partially-committed turn.** Per-program commit plus at-least-once
  task retry means a crashed turn's retry is a fresh wake over partly-committed
  state. Mitigation: write capabilities carry capability-layer idempotency keys,
  so a re-run cannot double-send; reads re-run harmlessly.
- **Long-transaction contention is replaced, not eliminated.** Per-program
  commits bound each transaction to one program. A `statement_timeout` should be
  added to the engine as a backstop; none is configured today.
- **The research subagent reads untrusted content** — web pages, and the user's
  own mailbox. The two-mode split plus the typed, tainted finding contains it: a
  `web` run has outbound reach but no private data; a `personal` run has private
  data but no outbound reach; the finding cannot smuggle an instruction past the
  taint rail. Residual exposure — a `web` run steered to fetch attacker URLs —
  leaks only the research topic; the `cap.web.extract` host-safety check and
  egress controls, and the `action_attempts` ledger that records every fetch,
  bound it.
- **A long loop is not free.** Agent reliability degrades super-linearly with
  turn length. Mitigation: the wall-clock budget is moderate, not unbounded;
  stuck-detection halts runaway loops; the model sees its budget.
- **Worker pickup latency.** A user message now waits for the worker to take its
  task — at `worker_poll_seconds` (1.0s) this is sub-second; acceptable.
- **The HTTP contract changes.** The user-message endpoint returns `202`, not a
  reply. Non-Discord API clients must read the session-events endpoint. Ariel's
  surface is Discord; acceptable.
- **The single-threaded worker serializes all turns.** A long turn or research
  run delays other tasks for its duration. Acceptable at single-user volume; the
  lever if it bites is a second worker, not a second queue.

## Follow-ons (not in this cutover)

- Interim progress messages — a non-terminal `agent.emit_message` mid-loop,
  delivered immediately — become possible once delivery is a push and turns are
  async. Worth adding when long turns are common.
- Parallel research fan-out — a `web` run spawning several sub-searches and
  merging their findings — is the 1→N upgrade if single-run research becomes a
  bottleneck. Still one loop, one config; no orchestration layer.
- Typed, importable syscall stubs — signatures and docstrings the model imports
  inside its `run` program — so it writes correct calls on the first attempt.
- Evals and observability — trace review and failure analysis — are not in this
  cutover but should begin alongside it; the cutover adds no eval surface.
- Proactivity refinements the current SOTA suggests — a cheap pre-filter, an
  approval inbox, digest batching — stay out of scope; `modules/proactivity.md`
  forbids a triage tier, and at single-user wake volume that holds.

## Source Findings

This plan is based on: the May 2026 agent-loop SME survey (the butler/subagent
question, surveyed by parallel sub-agents over the execution spine, the
capability surface, the AI-call inventory, the repo rules, and the main-agent
context assembly); four focused dives (the `agency.run` async template and the
turn transaction model; run-program loop reusability and the read-capability
inventory; the turn lifecycle and the menu of options for a long loop; the
sandbox process model and `emit_value`); and a ten-sub-agent web verification of
the direction against the 2025-2026 state of the art (durable agent runtimes,
context engineering, the code-execution tool interface, deep-research agents,
multi-agent consensus, personal-assistant products, proactive agents, agent-loop
design, agent security, and practitioner consensus). The verification found the
direction validated and in places ahead of the field; its one material finding —
the research subagent's lethal-trifecta exposure — is resolved by the two-mode
split specified above.
