# Agent Loop

## Scope

This document owns Ariel's agent loop: the run-program reason→act→observe loop,
its configured drivers — the main agent, research subagent, memory retriever,
and memory rememberer — async worker-run turns and Discord delivery, the long
adaptive loop, the per-turn scratch store, and per-program commit.

The agent loop follows [../ai-first.md](../ai-first.md): the model owns every
judgment within a turn; deterministic code owns the rails — the sandbox, the
syscall boundary, the budget, stuck-detection, commit, and delivery.

## The run-program loop

The model's entire tool surface is one tool, `run`: it authors a Python `run`
program and the host executes it. A turn is a loop of such programs.

Each round: the host calls the model with the running context; the model
returns exactly one `run` call whose argument is a Python program;
`execute_run_program` (`run_runtime.py`) runs that program in the persistent
gVisor sandbox. Every effect in the program is a namespaced syscall to a host
callable. The host dispatches each syscall, feeds its result back into the
program, and when the program finishes feeds the program's outcome back into
the model's context for the next round. The loop ends when the model emits its
terminal output or a rail stops it.

A syscall is one of three kinds. The main-loop `agent.*` output syscalls
(`agent.emit_message`, `agent.emit_value`, `agent.pause_until_input`) and the
two `scratch.*` store syscalls are handled inline in `run_runtime.py` — they are
not capabilities. Every other syscall is a capability call routed through
`process_one_call`, the unchanged per-call lifecycle: policy, taint, approval,
egress, output guardrails, and the `action_attempts` audit ledger. The model
never reaches a capability except as a syscall, and deterministic code never
routes between capabilities — the program decides.

`agent.emit_finding` and `agent.emit_done` are non-main-loop output syscalls,
eligible only in research and memory-subsystem runs.

Taint accumulates within a program: a syscall that returns
untrusted-influenced content advances the program's runtime provenance, so
every later syscall in that program is evaluated with it. A program returns its
taint delta; the loop merges that into the turn baseline so the next program
sees it (`_merge_runtime_provenance` in `app.py`). Taint crosses programs;
nothing inside a turn launders it.

## Loop Drivers

`run_agent_loop` (`agent_loop.py`) is the shared reason→act→observe loop. Each
driver assembles its context and `LoopConfig`, calls the shared loop, then maps
`LoopResult` to its own persistence and delivery contract:

- **The main agent** — `_wake` in `app.py`. It owns the user-facing
  conversation and every write. It assembles memory and eligibility context,
  runs the loop on every eligible capability (writes gated by
  `requires_approval`), and ends when the model calls `agent.emit_message`.
- **The research subagent** — `run_research` in `research_runtime.py`. The same
  loop structure, driven read-only on one research mode's capability whitelist,
  ending on `agent.emit_finding`.
- **The memory retriever** — `run_retriever` in `memory.py`. It searches and
  reads the memory substrate, then emits one `recall_v1` finding.
- **The memory rememberer** — `run_rememberer` in `memory.py`. It runs in
  `memory_encode` or `memory_dream` mode, applies `memory.note.*` operations,
  and exits with `agent.emit_done`.

The drivers differ by eligible callables, wall-clock budget, terminal output,
prompt version, judgment recording, and persistence. Everything else — the
model-call backstop, stuck-detection, scratch store, `emit_value` eviction,
round-history eviction, per-program commit, retry, and run-protocol validation
— lives once in `run_agent_loop`.

## Async worker-run turns and delivery

Every turn runs in the single-threaded background worker, reached through the
`background_tasks` queue. The HTTP ingress never runs a turn.

The user-message endpoint (`post_message`, `POST /v1/sessions/{id}/message`)
validates its input, enqueues a `user_message` task, and returns `202` with
`{status: "accepted", task_id}`. The worker takes the earliest due row,
dispatches on `task_type`, and runs the turn:

- `user_message` and `agent_wake` — the worker builds a `WakeContext` and calls
  `_wake`. `WakeContext` carries `trigger_kind`
  (`user_message`, `scheduled_task`, or `research_completion`), `prompt_text`,
  `discord_context`, `attachment_sources`, and `ingress_provenance`. The
  trigger kind is the only thing distinguishing a proactive wake from a user
  turn ([proactivity.md](proactivity.md)).
- `research_run` — the worker calls `run_research`, then enqueues an
  `agent_wake` carrying the finding (see below).

After a `_wake` turn commits, the worker delivers its emitted message:
`_deliver_to_discord` (`worker.py`) posts to the configured Discord channel
over the Discord REST API (`discord_channel_id`, `discord_bot_token`,
`discord_notification_timeout_seconds`). Pending approvals from the turn are
appended as approve/deny button rows. A silent turn delivers nothing. This is
one delivery path for every turn — a user reply, a proactive wake, a research
completion. Because every turn runs in the one worker, turns are serialized by
construction; there is no per-turn session advisory lock.

## The long adaptive loop

The loop runs as many reason→act→observe rounds as a task needs. There is no
fixed model-call cap. Three rails bound it:

- **Wall-clock budget** — `main_turn_budget_seconds` for `_wake`,
  `research_run_budget_seconds` for `run_research`. Checked before each model
  call; on exhaustion the loop ends gracefully — the main agent emits a plain
  "I wasn't able to finish that within the time available" message and the turn
  completes normally (not a `429`); a research run returns a `partial` finding.
- **Model-call backstop** — `agent_loop_max_model_calls`, a high paranoid cap,
  not the primary control. Exhausting it ends the loop on the same graceful
  path as budget exhaustion.
- **Stuck-detection** — if the model returns a program whose source is
  byte-identical to the immediately preceding round's source, the loop is
  cycling; it ends on the graceful path. This is host-side and the model cannot
  reason around it.

The model is told its remaining budget each round as a short `remaining budget:
Ns` system line, so it paces itself rather than running optimistically into the
limit. That line is rewritten in place each round, so only one copy
accumulates. The system-prompt prefix is held byte-stable across rounds (no
per-round timestamps in the prefix) to preserve the model provider's prompt
cache.

## Per-program commit and durability

A turn commits once per `run` program. After a clean program
(`RunProgramResult.program_ok`), the host commits its effects — action
attempts, events, emitted artifacts — before continuing or ending the turn.
There is no turn-spanning transaction; each transaction is one program long,
seconds not minutes.

A failed program is **not rolled back**. Its syscall-trace audit — the
`EventRecord` rows and the `action_attempts` ledger — is the audit spine and
commits regardless. What a failed program does not get to keep: its staged
approvals are voided (`_void_approvals` moves the approval and
its action attempt to `expired` so nothing surfaces as a live pending action),
and the program's emitted outputs — message, values, finding/done, pause — are
scrubbed from `RunProgramResult`. The model is fed the program error and
authors the next program.

A turn is not program-journaled. If the worker crashes after committing any turn
state but before deleting the `background_tasks` row, the retried task looks up
`turns.source_background_task_id`. A completed or failed source turn is reused;
an interrupted `in_progress` source turn is marked failed and the model is not
called again. Committed programs' effects stand; write capabilities still carry
capability-layer idempotency keys, but crash replay does not intentionally run a
second model turn.

Direct HTTP user-message ingress also has request idempotency above the queue:
`turn_idempotency_keys` stores the pending `202` response by `Idempotency-Key`
and is updated with the completed turn response after the worker commits. A
retry after the transient queue row is gone replays that durable response instead
of creating a second `user_message` task.

## The scratch store

A turn gets a host-side, per-turn **scratch store** — a `dict[str,
ScratchEntry]` threaded through the loop and into `execute_run_program`. Two
inline syscalls reach it:

- `scratch.set(key, value)` — stores `value` host-side under `key`.
- `scratch.get(key)` — returns the stored value into the program.

Each stored entry carries the taint of the program that set it; `scratch.get`
re-applies that taint, so untrusted data carried across programs stays tainted.
The store is bounded host-side — 64 entries, 512 KiB per value, 4 MiB total —
and lives for the turn only; it is not memory.

Large intermediate data — search results, fetched pages, mailbox extracts —
lives in the scratch store as host-side values. Capability outputs are not
auto-echoed into later model rounds. The only values that enter later model
context are the facts the program deliberately surfaces with
`agent.emit_value`. As a backstop, superseded `emit_value` rounds are evicted
from the model's input items: only the most recent `emit_value` round is
retained.

## The research subagent

The main agent dispatches breadth-first, read-heavy, independent investigation
to the research subagent. This is delegation without an orchestration layer:
there is no router and no manager — the model delegates by calling a syscall.

**Dispatch.** Inside a turn the main agent calls
`research.investigate(question, mode)`, backed by the `cap.research.investigate`
capability — `impact_level=read`, `policy_decision=allow_inline`, so dispatch
needs no approval. The capability's `execute` enqueues a `research_run` task
carrying the question, the mode, and the originating `session_id`, and returns
`{status: "queued", research_id}`. The main agent acknowledges to the user
("looking into that") and ends its turn. The main agent owns clarification — it
resolves an ambiguous request with the user before dispatching. The research
subagent cannot call `research.investigate`; delegation is single-level.

**The run.** The worker runs the `research_run` task through `run_research`,
which drives the read-only loop. The research prompt frames the question, the
mode, and the eligible read capabilities, and instructs the model to write its
sub-questions first, investigate, and call `agent.emit_finding` exactly once. The
run is persisted as a `TurnRecord` with `kind="research"` — no new table. Its
read syscalls write `action_attempts` rows like any turn; it stages no
approvals.

**The modes.** A research run is in exactly one mutually exclusive mode. It is
exposed to at most two of {untrusted input, private data, outbound reach}, never
all three.

- **`web`** — whitelist `RESEARCH_WEB_CAPABILITY_IDS`: `cap.search.web`,
  `cap.search.news`, `cap.web.extract`. Untrusted input and outbound reach; no
  private data.
- **`personal`** — whitelist `RESEARCH_PERSONAL_CAPABILITY_IDS`:
  `cap.email.search`, `cap.email.read`, `cap.drive.search`, `cap.drive.read`,
  `cap.calendar.list`. Private data and untrusted input (the mailbox is
  attacker-influenced); no outbound web reach.
- **`memories`** — whitelist `RESEARCH_MEMORIES_CAPABILITY_IDS`:
  `cap.memory.search`, `cap.memory.read`. Ariel-owned memory substrate only; no
  outbound web reach or live provider reads.

A run never holds multiple whitelists. A task needing more than one mode is split
into separate runs; the main
agent combines their findings — coupled synthesis stays with the single main
thread.

Web-mode fetches reach the network only through the existing `cap.web.extract`
and `cap.search.*` capabilities. Those capabilities carry the existing host
safety check — `_is_unsafe_web_extract_host` rejects loopback, private,
link-local, multicast, reserved, and single-label hosts — and the existing
egress controls. Every fetch is a capability syscall routed through
`process_one_call` and recorded in the `action_attempts` ledger. There is no
separate browsing service; the safety boundary is the capability rails.

**The finding.** The run terminates on `agent.emit_finding(summary, claims, gaps,
sources)`, returned as a typed `ResearchFinding`. `status` is one of three:

- `complete` — the run called `agent.emit_finding`.
- `partial` — the wall-clock budget, the model-call backstop, or
  stuck-detection ended the run before a finding. The loop ran cleanly; it just
  did not converge. The three lists are empty and `summary` is a short
  non-convergence note.
- `failed` — a model call raised. The lists are empty and `summary` is a short
  failure note.

`run_research` never raises for any of these exits. The host validates the
finding's shape lightly: `summary` is a string, and `claims`, `gaps`, and
`sources` are lists under a total-size bound. Their inner element shapes —
`claims` as `{statement, sources, confidence}`, `sources` as `{title,
reference, retrieved_at}` — are specified to the research model in its prompt,
not hard-validated host-side. This is deliberate: hard inner validation is
brittle, and the real containment is taint, not structure.

**The completion wake.** On completion the worker enqueues an `agent_wake`
carrying the finding. The wake renders the finding into the main agent's
context as a clearly-attributed block (`render_finding`) and carries **tainted**
`ingress_provenance` — the finding's text is model-authored over untrusted
content, so the main agent treats it exactly like a fetched web page. Because
the finding is tainted, any action it motivates is evaluated tainted and routes
through `requires_approval`. A prompt-injected finding cannot authorize an
unapproved action.

## Rules

- The model's tool surface is exactly `run`. Delegation is a syscall
  (`research.investigate`), never a second tool and never a deterministic
  route.
- `run_agent_loop` is the one shared reason→act→observe loop. `_wake`,
  `run_research`, `run_retriever`, and `run_rememberer` configure it and own
  their domain-specific setup and post-loop handling.
- `_wake` is the one trigger entrypoint. The research loop is a worker-task
  handler, not a trigger.
- Every turn runs in the single-threaded worker. The HTTP ingress enqueues and
  returns `202`; it never runs a turn. There is no per-turn session advisory
  lock.
- A turn commits per `run` program. There is no turn-spanning transaction and
  no program journaling. Task replay is keyed by `source_background_task_id`; it
  reuses or fails the source turn rather than running the model again. A failed
  program's syscall-trace audit commits; its staged approvals are voided and its
  emitted outputs scrubbed.
- The loop is bounded by a wall-clock budget the model can see, a model-call
  backstop, and host-side stuck-detection; budget or backstop exhaustion ends
  the loop gracefully.
- Large per-turn data lives in the scratch store; only keys and deliberate
  `emit_value` summaries enter the model's context. The scratch store is
  per-turn, taint-carrying, bounded, and is not memory.
- Every delivered message goes through the one worker-side Discord delivery
  path.
- The research subagent is strictly read-only, single-level, and runs in
  exactly one mode per run; a run never holds both private-data reads and
  open-web reach.
- A research finding is typed and is carried with tainted provenance;
  containment is the taint plus `requires_approval`, not the structure of the
  finding.
- Every capability syscall — main agent or research — passes the unchanged
  `process_one_call` rails.
