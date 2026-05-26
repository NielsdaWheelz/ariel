# Conversational Continuity — Cutover Spec (v2)

> **Status:** design spec, May 2026 (rewrite 2). v1 of this document
> proposed a multi-component curated design (transcript filter, IDs index,
> lifecycle eviction). That was rejected after review as deterministic
> judgment dressed up, contradicting `ai-first.md`. **v2 is the honest
> simple cutover:** one events-window block, no curation beyond a
> structural event-type whitelist, no new tables, no lifecycle machinery.
>
> **Mode:** hard cutover. No legacy code paths, no feature flags, no
> backward-compatibility shims. After cutover, the prior "stateless
> initial context" and the prior "cold-stop on budget" behaviours cannot
> be re-enabled.
>
> **Three coupled changes ship together as one cutover:**
>   1. **Events-window block** in every wake's initial context — the
>      last K externally-relevant events, content-agnostic, recency
>      + token budget only.
>   2. **Budget reformulation** — soft cap injects a wrap-up nudge to
>      the model; hard cap (much further out) still cold-stops as a
>      safety net.
>   3. **Session abolition** — `sessions`, `session_rotations`,
>      `session_id` columns, auto-rotation logic, and the
>      `_rotate_active_session` flow are deleted entirely. Last phase
>      so it doesn't block the events-window from shipping.
>
> **Doctrine stance:** the events-window block is *content-agnostic*
> selection (recency + capacity over a structural event-type whitelist).
> The whitelist criterion is "did this event represent something in the
> world or in the conversation, or is it loop trace?" — a structural
> distinction, not a high-signal judgment. No scoring, ranking, or
> selection-by-meaning anywhere in the new code path. The crystallization
> doctrine (`ai-first.md:186-189`) is not reopened.

---

## 1. Problem statement

The agent's initial context for every wake type (`user_message`,
`provider_sync`, `scheduled`, `research_completion`) is **stateless with
respect to prior turn contents.** Concrete IDs the agent has emitted,
surfaced, or returned — Gmail `message_id`, Calendar `event_id`, Drive
`file_id`, provider-evidence rows — are persisted durably in Postgres but
never surfaced into the next wake's initial messages. The retriever
subagent's `recall_v1` returns ≤200-char fuzzy snippets without canonical
IDs.

Consequence: when the user references something from a prior turn ("delete
both", "what was that", "send it to her"), the agent has no anchor. Its
only recovery is `memory.search` (fuzzy) or re-running tool reads, which
is non-deterministic, slow, and does not reliably converge.

Two compounding budget issues amplify the symptom:

- The agent loop cold-stops at `main_turn_budget_seconds = 180.0`. For a
  J.A.R.V.I.S.-class agent doing real work, this is far too short.
- When the budget is hit, the loop returns `budget_exhausted` and the
  user sees *"I wasn't able to finish that within the time available."*
  — without giving the model a chance to wrap up gracefully with what
  it has.

Documented failure: 2026-05-25 turn `trn_01ksh81hrrnz04k8zgkxtx42f6`.
User: *"try again (delete both. also, can we send new june homes to spam
or trash or something?)"*. The 22:01 provider-sync wake had surfaced two
June Homes message IDs to the user. Three hours later, the agent — unable
to see the prior surfaced IDs in its initial context — ran 18 Gmail
searches, hit three sandbox program errors, and was cold-stopped at 190s.
The user saw the budget fallback. Third consecutive failure on the same
request.

---

## 2. Goals

1. **The agent has access to its recent past.** Every wake's initial
   context includes the K most recent externally-relevant events from
   the agent's history (turn starts, assistant messages, tool execution
   outcomes, approvals, research findings, model failures, memory
   recalls, etc.), rendered as raw JSON-per-event in chronological
   order, capped by token budget. What the agent does with this is the
   agent's call.
2. **Long turns get a chance to wrap up.** When the soft time budget or
   model-call budget is crossed, the loop injects a system message
   asking the model to wrap up cleanly. Only the (much further) hard
   cap cold-stops the loop.
3. **Sessions cease to exist.** The `sessions` table, the
   `session_rotations` table, the `session_id` foreign keys on `turns`,
   `events`, `memory_log`, etc., the auto-rotation logic, and the
   `_rotate_active_session` code path are deleted. Turns and events
   become globally-scoped; scoping for queries is "last N turns" or
   "last N events" or "since timestamp," never "active session."
4. **No new threading machinery beyond the events-window block.** No
   new tables. No scorers, rankers, classifiers. No content
   judgment in the new code path. Pure recency + capacity.

## 3. Non-goals

The following are explicitly out of scope. Future work, separate spec.

- **Cross-history compaction / summarization.** The events-window is
  raw; summarization is the rememberer's job (writes `memory_notes`).
- **Long-horizon memory / multi-day continuity.** That's `memory.recall`
  on `memory_log` and `memory_notes`. Untouched by this cutover.
- **Generic entity extraction (people, projects, companies).** No.
- **Per-event signal scoring.** Forbidden by ai-first.
- **A new agent-facing capability.** The events block is read-only system
  context; the agent uses existing capabilities to refetch by canonical
  IDs when it wants more detail.
- **Replacing the retriever / `recall_v1`.** Still runs, still produces
  fuzzy semantic recall. The two paths are complementary.
- **Within-turn compaction.** `_evict_oldest_round` is untouched.

## 4. Background — current state (cited)

### 4.1 Wake input composition

Four wake types, single entrypoint (`src/ariel/app.py:1275` → `_wake`).
All four pass through `_build_initial_messages` (`app.py:396`) which
emits a fixed stack of system blocks: `policy_system_instructions`,
`discord_context`, `eligible_callables`, `tool_surface_facts`,
`turn_ref`, `recall_v1`, `open_jobs`, `recent_artifacts`.

**No system block today carries prior assistant messages, prior tool
outputs, or canonical IDs from earlier turns.** `runtime_provenance`
(`app.py:950-992`) tracks only taint markers.

### 4.2 Event surface

The `events` table is the single durable record of what happened in each
turn. Schema (`db.py`):

| Column | Type | Purpose |
|---|---|---|
| `id` | varchar(32) | PK |
| `session_id` | varchar(32) | FK to sessions (**dies in Phase 5**) |
| `turn_id` | varchar(32) | FK to turns |
| `sequence` | int | Within-turn ordering |
| `event_type` | varchar(64) | One of ~41 canonical values |
| `payload` | jsonb | Event-specific data |
| `created_at` | timestamptz | UTC timestamp |

Current full event_type catalogue (41 values, ordered alphabetically):

```
evt.action.approval.approved        evt.action.approval.denied
evt.action.approval.expired         evt.action.approval.requested
evt.action.call_denied              evt.action.execution.failed
evt.action.execution.retrying       evt.action.execution.started
evt.action.execution.succeeded      evt.action.policy_decided
evt.action.proposed                 evt.agent.output_not_applied
evt.agent.premature_synthesis_rejected
evt.agent.value_emitted             evt.ai_judgment.completed
evt.ai_judgment.failed              evt.assistant.emitted
evt.connector.google.connect.failed evt.connector.google.connect.started
evt.connector.google.connect.succeeded
evt.connector.google.disconnected   evt.connector.google.reconnect.failed
evt.connector.google.reconnect.started
evt.connector.google.reconnect.succeeded
evt.connector.google.refresh.failed evt.connector.google.refresh.succeeded
evt.memory.recalled                 evt.memory.recall_failed
evt.model.completed                 evt.model.failed
evt.model.protocol_failed           evt.model.started
evt.provider_write.receipt_reconciled
evt.provider_write.reconcile_unavailable
evt.research.failed                 evt.research.finding_emitted
evt.research.partial                evt.research.started
evt.run.validation_failed           evt.turn.completed
evt.turn.failed                     evt.turn.started
```

### 4.3 Budget facts

| Setting | Current value | File:line |
|---|---|---|
| `main_turn_budget_seconds` | 180.0 | `config.py:136` |
| `agent_loop_max_model_calls` | 50 | `config.py:142` |
| Wall-clock cold-stop branch | yes | `agent_loop.py:318-322` |
| Soft cap / wrap-up nudge | **does not exist** | — |
| `max_response_tokens` | 12,000 | `config.py:135` |

The current behaviour is binary: under-budget → continue, over-budget →
return `outcome="budget_exhausted"` immediately.

### 4.4 Session footprint (what dies in Phase 5)

Tables FK-referencing `sessions(id)`:

```
turns.session_id                events.session_id
memory_log.session_id           action_attempts (via turns)
approval_requests (via turns)   artifacts (via turns)
attachment_sources (via turns)  captures (via turns)
jobs (via turns)                turn_idempotency_keys
session_rotations.from_session_id, session_rotations.to_session_id
```

Settings to delete:

```
config.py:133  auto_rotate_max_turns: int = 120
config.py:134  auto_rotate_max_age_seconds: int = 172800
```

Code paths to delete:

```
_rotate_active_session         _auto_rotation_reason
session_rotations recording    "active session" lookups in app.py
```

The `approval_actor_id` setting (also in `config.py`) is **kept** — it's
the actor binding, unrelated to sessions.

### 4.5 Token budget for the new block

MAIN model is `anthropic/claude-sonnet-4.6` with a **1M-token context
window** (`models.py:27`, Anthropic Sonnet 4.6 announcement). Current
typical initial prompt is ~1,200 tokens (0.12% of capacity). Total typical
turn usage ~17K tokens. Headroom for an events block is effectively
unconstrained at any realistic scale.

---

## 5. Target behaviour (end-state, by example)

### 5.1 Scenario A — pronoun reference across the gap

1. **22:01** Provider-sync wake. Gmail push delivers 2 new messages from
   accounting@junehomes.com. Agent says *"June Homes just sent an unread
   balance reminder…"*. The turn's events include: `evt.turn.started`
   (with wake context), one or more `evt.action.execution.succeeded`
   rows (the `cap.email.read` calls that resolved the evidence),
   `evt.assistant.emitted` (with the reply text), `evt.turn.completed`.
2. **04:17 next morning** User-message wake: *"try again (delete
   both…)"*. Initial context now includes a new **`recent_events`
   system block**: the last K externally-relevant events from the
   `events` table (most recent first or chronological; chronological is
   our pick — see §6.4), capped by token budget. The 22:01 turn's
   succeeded events are in the block — including the canonical
   `message_id`s for the two June Homes messages.
3. Agent reads the block, identifies the two `message_id`s in the 22:01
   `evt.action.execution.succeeded` payloads, and calls
   `email.trash(message_ids=[…], idempotency_key=…, user_instruction_ref=turn:<this_turn>)`
   in its first or second round.

There is no curated transcript, no IDs index, no `turn_surfaced_evidence`
table. The agent does its own pattern-matching across raw events.

### 5.2 Scenario B — soft budget wrap-up

1. A complex multi-mailbox request takes the agent through 80 model
   calls and 11 minutes of wall-clock. Halfway through, the agent has
   useful partial results.
2. At **wall-clock 600s** (`turn_budget_seconds_soft`) **or** at
   **model_call_count 120** (`agent_loop_max_model_calls_soft`), the
   loop injects a system message *before* the next model call:
   *"You are past your soft budget. Wrap up: emit a final
   `agent.emit_message` to the user with what you have — partial
   results, what you tried, what remains. Do not start new long tool
   chains. If more work is needed, name it as a follow-up in your
   wrap-up message."* The nudge is injected exactly once per turn.
3. The agent has up to the **hard** budgets
   (`turn_budget_seconds_hard = 1800`, `agent_loop_max_model_calls_hard = 300`)
   to emit that final message. If it does, the turn ends as
   `outcome="message"` with the agent's text — not as
   `budget_exhausted`.
4. Only if the hard cap is then breached does the loop cold-stop with
   the existing `budget_exhausted` outcome.

### 5.3 Scenario C — sessions are gone

After Phase 5: `sessions` table dropped, `session_id` columns dropped
from all tables, `_rotate_active_session` deleted,
`auto_rotate_*` settings removed. The events-window block's query is
now "last K externally-relevant events" with no session filter. Taint
lookback is "last 12 turns" (a fixed count, not a session). Idempotency
becomes time-bounded (TTL on `turn_idempotency_keys`). Discord DMs
create turns directly with no parent session.

---

## 6. Architecture

### 6.1 The single new initial-context block

One new function:

```python
# src/ariel/conversational_continuity.py

def build_recent_events_block(
    *,
    db: Session,
    current_turn_id: str,
    settings: AppSettings,
    now: datetime,
) -> str | None:
    """Render the last K externally-relevant events as a system block.
    Returns None if no eligible events exist."""
```

Implementation:

1. Query `events` rows where:
   - `event_type` is in `EXTERNAL_EVENT_TYPES` (whitelist, see §6.2)
   - `created_at < now`
   - (Phase 0–4) `session_id = <active session>` for the current turn.
     (Phase 5) **no session filter** — drop the `session_id` column entirely.
2. Order chronologically (`created_at ASC, sequence ASC`).
3. Take the latest K rows that fit within the token budget
   (`recent_events_token_budget`, default 100,000 tokens).
4. For each row, render `{id, created_at, turn_id, event_type, payload}`
   as a single JSON line. If the rendered line exceeds
   `recent_event_payload_byte_cap` (default 4,096 bytes), apply the
   compact-view rule (§6.3).
5. Concatenate, prepend a one-line header (§6.4), return.

If a single event's compact view still exceeds the per-event cap, render
its bare metadata (`id`, `event_type`, `created_at`, `turn_id`) and a
single `"_oversize": true` marker. The agent can fetch the full row via
the existing memory pathway if needed — see §6.6.

### 6.2 `EXTERNAL_EVENT_TYPES` — the whitelist

The criterion: *did this event represent something that happened in the
world or in the conversation, or is it loop trace?* Loop trace is
excluded. The list is checked into `conversational_continuity.py` as a
frozen set. Reviewers add to it when introducing a new event_type that
represents a real state change.

**Included (21 event_types):**

```
evt.turn.started                         evt.turn.completed
evt.turn.failed                          evt.assistant.emitted
evt.action.execution.succeeded           evt.action.execution.failed
evt.action.approval.requested            evt.action.approval.approved
evt.action.approval.denied               evt.action.approval.expired
evt.action.call_denied                   evt.run.validation_failed
evt.research.finding_emitted             evt.research.failed
evt.research.partial                     evt.connector.google.disconnected
evt.connector.google.reconnect.succeeded evt.model.failed
evt.model.protocol_failed                evt.provider_write.receipt_reconciled
evt.memory.recalled
```

**Excluded (20 event_types — loop trace, timing, redundant markers,
intra-turn debris):**

```
evt.model.started                         evt.model.completed
evt.action.proposed                       evt.action.policy_decided
evt.action.execution.started              evt.action.execution.retrying
evt.agent.value_emitted                   evt.agent.output_not_applied
evt.agent.premature_synthesis_rejected    evt.ai_judgment.completed
evt.ai_judgment.failed                    evt.research.started
evt.connector.google.connect.failed       evt.connector.google.connect.started
evt.connector.google.connect.succeeded    evt.connector.google.reconnect.failed
evt.connector.google.reconnect.started    evt.connector.google.refresh.failed
evt.connector.google.refresh.succeeded    evt.memory.recall_failed
evt.provider_write.reconcile_unavailable
```

Rationale notes per excluded type:

- `evt.model.*`: timing/usage payloads only; nothing the next-turn agent
  cares about.
- `evt.action.proposed`: superseded by `succeeded` / `failed`.
- `evt.action.execution.started`: pure marker; the outcome event is the
  one that carries information.
- `evt.action.execution.retrying`: noise during transient failures.
- `evt.action.policy_decided`: gate ack; the resulting `succeeded` /
  `call_denied` carries the information.
- `evt.agent.value_emitted`: within-turn reasoning artifact; design
  contract is that emit_value rounds are evicted after their successor
  round (`agent-loop.md:174-179`).
- `evt.ai_judgment.*`: loop bookkeeping for the rememberer/retriever
  side.
- `evt.connector.google.connect.*` and `refresh.*`: routine connector
  lifecycle; only `disconnected` and `reconnect.succeeded` represent
  visible state changes.
- `evt.research.started`: announce-only; `finding_emitted` /
  `failed` / `partial` carry the substance.
- `evt.memory.recall_failed`: an absence-of-data signal; the next-turn
  agent learns it differently via missing `recall_v1` entries.

### 6.3 Compact-view rule for oversized payloads

Triggered when a serialized event row exceeds
`recent_event_payload_byte_cap`. The rule is a deterministic structural
transform — no content judgment.

1. Always include the top-level scalar fields of the payload: any value
   that is `str`, `int`, `float`, `bool`, or `None`.
2. For any value that is a list or dict, replace it with a structural
   marker:
   - If the field name matches a canonical-ID pattern
     (`*_id`, `*_ids`, `message_id`, `thread_id`, `event_id`, `file_id`,
     `provider_object_ids`, `provider_event_ref`), keep the IDs (they
     are the agent's lookup keys).
   - Otherwise replace with `{"_kind": <"list"|"dict">, "_size": <count>, "_byte_size": <bytes>}`.
3. Insert a top-level marker `"_truncated": true` to flag the
   transformation.
4. The agent fetches full content by re-issuing the relevant capability
   call using the preserved IDs (`email.read(message_id=…)`,
   `calendar.list(…)`, `drive.read(file_id=…)`, etc.). No new syscall.

The rule lives in `_compact_event_payload(payload: dict, *, cap: int) -> dict`
in `conversational_continuity.py`. Pure function, unit-tested with
fixtures for each capability family.

### 6.4 Block rendering format

Header (one line):

```
recent_external_events (last K events you have access to, chronological,
oldest first; loop-trace events are filtered out at the system level).
each line is one event row as JSON: {id, created_at, turn_id, event_type, payload}.
the agent uses canonical IDs in payloads to re-fetch full content via existing
capabilities (email.read, calendar.list, drive.read, etc.).
```

Then one JSON object per line (newline-separated), e.g.:

```
{"id":"evt_01...","created_at":"2026-05-25T22:01:14Z","turn_id":"trn_...","event_type":"evt.turn.started","payload":{"wake_kind":"provider_sync","user_message":"Provider sync wake: Google Gmail\n\nGoogle Gmail sync found 2 new inbound messages..."}}
{"id":"evt_01...","created_at":"2026-05-25T22:01:15Z","turn_id":"trn_...","event_type":"evt.action.execution.succeeded","payload":{"capability_id":"cap.email.read","action_attempt_id":"aat_...","status":"succeeded","execution_output":{"schema_version":"google.gmail.message_evidence.v1","message":{"message_id":"19c638912663c9e5","subject":"Reminder: $X balance due","sender":"accounting@junehomes.com",...}}}}
{"id":"evt_01...","created_at":"2026-05-25T22:01:18Z","turn_id":"trn_...","event_type":"evt.assistant.emitted","payload":{"text":"June Homes just sent an unread balance reminder..."}}
{"id":"evt_01...","created_at":"2026-05-25T22:01:18Z","turn_id":"trn_...","event_type":"evt.turn.completed","payload":{"outcome":"message"}}
...
{"id":"evt_01...","created_at":"2026-05-26T04:17:47Z","turn_id":"trn_<this>","event_type":"evt.turn.started","payload":{"wake_kind":"user_message","user_message":"try again (delete both. also, can we send new june homes to spam or trash or something?)"}}
```

The current turn's `evt.turn.started` is intentionally **included** as
the last line, so the agent sees its own user prompt in the same stream
as prior context — no inconsistency between block content and user
prompt.

Block ordering in the system stack: after `recall_v1`, before
`open_jobs`. The user is the only authoritative source on this; we
follow the v1 placement choice (which the user has not objected to).

### 6.5 Selection: K events, with token budget

Concretely:

1. Token budget: `recent_events_token_budget` (default **100,000
   tokens** = 10% of Sonnet-4.6's 1M context window). Estimated at 4
   bytes per token (English/JSON conservative).
2. Hard count cap: `recent_events_max_rows` (default **5,000**). Safety
   net so a runaway DB query can't load arbitrarily many rows.
3. Algorithm: query the most recent N rows (where N is the count cap),
   render them, total the bytes, drop from the *oldest* end until the
   total fits within budget. If after dropping all but the most recent
   row the budget still overflows, render only the current turn's
   `evt.turn.started` (always fits).
4. The query is a single index-backed `SELECT * FROM events WHERE event_type = ANY($1) ORDER BY created_at DESC LIMIT $2`. A
   covering index on `(event_type, created_at)` is added in Phase 1.

### 6.6 Re-fetching by canonical ID

When an oversized event has been compact-rendered, the agent reads the
canonical IDs and re-fetches via the **existing** capability surface
that produced them:

| Original capability | Re-fetch capability |
|---|---|
| `cap.email.search` (large `messages[]`) | `cap.email.read(message_id=…)` per message |
| `cap.email.read` (large `body`) | `cap.email.read(message_id=…, mode="message")` again — fetches the latest version |
| `cap.calendar.list` (long event lists) | `cap.calendar.list(window_start=…, window_end=…)` with narrower window |
| `cap.drive.read` (large file content) | `cap.drive.read(file_id=…)` |
| `cap.web.extract` (large doc) | `cap.web.extract(url=…)` |
| provider_evidence (large body) | `cap.email.read(message_id=<external_id>)` |

No new syscall. The agent already has every capability needed.

---

## 7. Budget reformulation

### 7.1 New settings (added in Phase 3; `config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `turn_budget_seconds_soft` | 600.0 | Wall-clock soft cap (10 min); inject wrap-up nudge. |
| `turn_budget_seconds_hard` | 1800.0 | Wall-clock hard cap (30 min); cold-stop with `budget_exhausted`. |
| `agent_loop_max_model_calls_soft` | 120 | Model-call soft cap; inject wrap-up nudge. |
| `agent_loop_max_model_calls_hard` | 300 | Model-call hard cap; cold-stop. |

The current `main_turn_budget_seconds` (180.0) and
`agent_loop_max_model_calls` (50) settings are **deleted** — there is
no path that uses them after this phase.

### 7.2 Wrap-up nudge mechanism (in `agent_loop.py`)

Replace the current binary check (`agent_loop.py:318-322`):

```python
# CURRENT
if elapsed_s > cfg.budget_seconds or model_call_count > cfg.max_model_calls:
    return _budget_exhausted_result(...)
```

with:

```python
# NEW
elapsed_s = time.perf_counter() - loop_started_at

if elapsed_s > cfg.budget_seconds_hard or model_call_count > cfg.max_model_calls_hard:
    return _budget_exhausted_result(
        model_call_count=model_call_count,
        created_action_attempt_count=created_action_attempt_count,
        final_runtime_provenance=final_runtime_provenance,
    )

if not wrap_up_nudge_injected:
    if elapsed_s > cfg.budget_seconds_soft or model_call_count > cfg.max_model_calls_soft:
        messages.append(_system_message(WRAP_UP_NUDGE_TEXT))
        add_event(
            "evt.agent.wrap_up_nudged",
            {
                "elapsed_seconds": elapsed_s,
                "model_call_count": model_call_count,
                "soft_budget_seconds": cfg.budget_seconds_soft,
                "soft_budget_model_calls": cfg.max_model_calls_soft,
            },
        )
        wrap_up_nudge_injected = True
```

The wrap-up text (constant in `agent_loop.py`):

```
WRAP_UP_NUDGE_TEXT = (
    "You are past your soft turn budget. Wrap up now: emit one "
    "agent.emit_message to the user with what you have — partial "
    "results, what you tried, and what remains. Do not start new "
    "long tool chains or new research investigations. If more work "
    "is genuinely needed, name it as a follow-up in your wrap-up "
    "message; do not silently continue."
)
```

A new event_type, `evt.agent.wrap_up_nudged`, is added to the audit
trail. **It is excluded from the events-window whitelist** (it's loop
trace).

### 7.3 LoopConfig changes

```python
# Before
@dataclass(slots=True, frozen=True)
class LoopConfig:
    ...
    budget_seconds: float
    max_model_calls: int

# After
@dataclass(slots=True, frozen=True)
class LoopConfig:
    ...
    budget_seconds_soft: float
    budget_seconds_hard: float
    max_model_calls_soft: int
    max_model_calls_hard: int
```

Caller (`app.py:1511-1512`) updates to pass all four. Same for the
research loop and memory loops (they get the same soft/hard treatment;
research budgets stay smaller — see §10).

---

## 8. Session abolition (Phase 5)

### 8.1 What goes

**Tables dropped:**

```sql
DROP TABLE session_rotations;
DROP TABLE sessions;
```

**Columns dropped:**

```sql
ALTER TABLE turns           DROP COLUMN session_id;
ALTER TABLE events          DROP COLUMN session_id;
ALTER TABLE memory_log      DROP COLUMN session_id;
ALTER TABLE turn_idempotency_keys DROP COLUMN session_id;
-- (and any other table found to carry session_id)
```

**Indexes dropped** (whatever was keyed by `session_id`), replaced with
indexes on `created_at` for recency queries.

**Settings dropped:**

```python
config.py: auto_rotate_max_turns          # delete
config.py: auto_rotate_max_age_seconds    # delete
```

**Code deleted:**

```python
# src/ariel/app.py
_rotate_active_session(...)
_auto_rotation_reason(...)
# every `active_session = db.scalar(... sessions ...)` lookup
# every `session_id = active_session.id` reference
# the entire "active session" concept in _wake
```

**Db model deleted:**

```python
# src/ariel/db.py
class SessionRecord(Base): ...        # delete
class SessionRotationRecord(Base): ...# delete
```

### 8.2 What replaces session-scoped queries

| Old query | New query |
|---|---|
| "turns in the active session" | "the most recent N turns" (N derived from need) |
| "events in the active session" | "events in the last N turns" |
| Taint lookback over `prior_turns[-_TAINT_LOOKBACK_TURNS:]` | unchanged — already a count-based slice |
| "active session id for actor binding" | drop binding; use `approval_actor_id` setting directly |
| "session-scoped idempotency keys" | global idempotency keys with TTL (default 24h) |
| `session_id` foreign keys on records | dropped, no replacement; records become globally scoped |

### 8.3 Discord DM flow after sessions

`worker.py` Discord intake creates a `TurnRecord` with no `session_id`.
The events-window query orders by `created_at`. Routing is identical
otherwise. Idempotency on incoming messages already lives in
`turn_idempotency_keys` (its primary key is `(idempotency_key, actor_id)`
in the post-cutover form, not session-scoped).

### 8.4 Why kill sessions

Sessions are a deterministic-code construct from the original chat-app
mental model. The agent never reasons about them. `memory.recall`
already cross-cuts sessions. Auto-rotation is invented complexity that
serves no user-visible purpose; it just forces queries to filter by an
arbitrary unit. Removing the construct simplifies the events-window
query (drops a join), simplifies idempotency, simplifies actor
binding, and removes ~500 lines of code.

The doctrine: **sessions are a deterministic judgment about where the
conversation breaks. The judgment is wrong (no break exists at the
agent's level). Delete it.**

### 8.5 Acceptance for Phase 5

- All Phase 1–4 tests still pass with the session column dropped.
- A new integration test confirms a Discord DM flow with no session
  scaffolding behaves identically end-to-end.
- `grep -rn "session_id\|SessionRecord\|active_session" src/ariel/`
  returns zero matches after the migration runs (modulo comments
  intentionally referencing the former concept in cutover docs).

---

## 9. Files — touch list

### 9.1 New files

- `src/ariel/alembic/versions/<ts1>_add_events_index_for_window.py` —
  index on `events(event_type, created_at)` for the new query.
- `src/ariel/alembic/versions/<ts2>_drop_sessions.py` (Phase 5) — DDL
  for §8.1.
- `src/ariel/conversational_continuity.py` — module owning
  `build_recent_events_block`, `_compact_event_payload`,
  `EXTERNAL_EVENT_TYPES`.
- `tests/unit/test_conversational_continuity.py` — unit tests for
  block rendering, whitelist, compact-view rule, token-cap eviction.
- `tests/integration/test_recent_events_acceptance.py` — end-to-end
  rerun of the documented failure scenario.
- `tests/integration/test_budget_wrap_up.py` — exercises the soft-budget
  wrap-up path.
- `tests/integration/test_sessions_removed.py` (Phase 5) — confirms
  no session footprint remains.

### 9.2 Modified files

- `src/ariel/app.py` — wire the new block into
  `_build_initial_messages` (one call to
  `build_recent_events_block`, one new SystemPromptPart). Drop
  session lookups (Phase 5).
- `src/ariel/agent_loop.py` — replace cold-stop with soft/hard split
  and the wrap-up nudge (§7.2). Add `evt.agent.wrap_up_nudged`
  emission.
- `src/ariel/config.py` — add four new budget settings; delete
  `main_turn_budget_seconds`, `agent_loop_max_model_calls`,
  `auto_rotate_max_turns`, `auto_rotate_max_age_seconds`; add
  `recent_events_token_budget`, `recent_events_max_rows`,
  `recent_event_payload_byte_cap`.
- `src/ariel/db.py` — drop `SessionRecord`, `SessionRotationRecord`
  models; drop `session_id` columns from affected models (Phase 5).
- `src/ariel/worker.py` — remove session creation/rotation in the
  Discord/provider-sync intake paths.
- `docs/modules/agent-loop.md` — document the new block, the soft
  wrap-up, the session removal.
- `docs/jarvis-system-prompt.md` — one paragraph in the agent
  framing: *"You have a `recent_external_events` block containing
  the canonical record of recent state changes in your history. Use
  it to resolve referents; canonical IDs in payloads are valid
  inputs to the corresponding capabilities."*

### 9.3 Deleted code (hard cutover, no shims)

- `_rotate_active_session`, `_auto_rotation_reason`,
  `_record_session_rotation` (Phase 5).
- The "active session" lookup pattern wherever it appears (Phase 5).
- The current `_budget_exhausted_result` path is kept (still used by
  the hard cap) but the soft-cap call site is replaced with the
  wrap-up nudge.
- `main_turn_budget_seconds` and `agent_loop_max_model_calls` settings
  are deleted outright (no aliasing, no warning, no deprecation
  cycle).

---

## 10. Capability contract / API design

### 10.1 No new model-facing capabilities

The events block is a read-only system block. The agent already has
every tool needed to act on canonical IDs surfaced in it.

### 10.2 Internal Python API

```python
# src/ariel/conversational_continuity.py

EXTERNAL_EVENT_TYPES: frozenset[str] = frozenset({
    "evt.turn.started",
    "evt.turn.completed",
    # ... (21 entries; see §6.2)
})

def build_recent_events_block(
    *,
    db: Session,
    current_turn_id: str,
    settings: AppSettings,
    now: datetime,
) -> str | None: ...

def _compact_event_payload(payload: dict[str, Any], *, cap: int) -> dict[str, Any]:
    """Pure structural transform; idempotent on small payloads."""
```

### 10.3 Settings contract

All new knobs are in `AppSettings` (`config.py`), env-overridable. Defaults
tuned for Sonnet-4.6 1M context + a J.A.R.V.I.S.-class workload.

| Setting | Default | Bound | Notes |
|---|---|---|---|
| `recent_events_token_budget` | 100,000 | ≥ 1,000 | 10% of 1M context |
| `recent_events_max_rows` | 5,000 | ≥ 1 | Hard count safety net |
| `recent_event_payload_byte_cap` | 4,096 | ≥ 512 | Per-event truncation threshold |
| `turn_budget_seconds_soft` | 600.0 | > 0, < hard | Wrap-up trigger |
| `turn_budget_seconds_hard` | 1800.0 | > soft | Cold-stop |
| `agent_loop_max_model_calls_soft` | 120 | ≥ 1, < hard | Wrap-up trigger |
| `agent_loop_max_model_calls_hard` | 300 | > soft | Cold-stop |

### 10.4 LoopConfig contract

`run_agent_loop`'s `cfg: LoopConfig` carries the four new budget fields
(no more `budget_seconds` / `max_model_calls`). All callers updated in
Phase 3:

- `app.py:1506` (main agent loop)
- `memory.py:639, 885` (memory subagent loops)
- `research_runtime.py:443` (research loop)

For research and memory loops we want **smaller** budgets — research is
intentionally narrower than the main loop. Concrete defaults:

| Loop | soft seconds | hard seconds | soft calls | hard calls |
|---|---|---|---|---|
| main (`_wake`) | 600 | 1800 | 120 | 300 |
| research | 180 | 600 | 40 | 100 |
| memory (recall) | 60 | 180 | 20 | 60 |
| memory (encode) | 60 | 180 | 20 | 60 |
| memory (dream) | 180 | 600 | 60 | 150 |

Each loop's caller picks the right pair from settings. The settings
themselves are top-level (not per-loop) because the loop's identity is
already known at the call site; per-loop branching in code is judgment
we already accept.

---

## 11. Composition with existing systems

### 11.1 With `recall_v1` / retriever

**Complementary.** `recall_v1` is semantic/fuzzy across the rememberer's
durable notes. `recent_events` is canonical/recency across the audit
log. The agent has both; it learns which is which from the block
headers. The retriever's prompt is updated in Phase 6 with one
clarifying sentence: *"items already visible in
`recent_external_events` need not be re-recalled."*

### 11.2 With `runtime_provenance` / taint

**Unchanged.** `runtime_provenance` continues to carry taint markers
across turns. The events block's payloads are themselves the same
tainted content the rest of the system already handles; rendering them
into a system block does not change their taint status. Mutating
actions stay flagged as influenced-by-untrusted-content via the
existing mechanism.

### 11.3 With provider sync

**Unchanged.** Provider sync continues to create `provider_evidence`
rows and emit `evt.action.execution.succeeded` events for the
`cap.email.read` calls that resolve them. Those `succeeded` events are
in the whitelist, so the next user-message wake sees the canonical
`message_id`s naturally.

### 11.4 With session rotation

**Replaced by deletion** (Phase 5). After Phase 5, there is no session
rotation. The events-window query simply orders by `created_at`. Long
horizons fall off the budget; `memory.recall` and `memory.search` are
the cross-horizon path.

### 11.5 With the agent loop / round budget

**Tighter coupling, but cleanly named.** The agent-loop's per-round
eviction (`_evict_oldest_round`, `agent_loop_live_rounds=8`) is
unchanged. The new block ships in the stable prefix; it is never
evicted within a turn. The new soft/hard budgets replace the single
budget knob.

### 11.6 With the rememberer / memory writes

**Side benefit.** The rememberer subagent's prompt context grows by
the same events block; it sees clearer recent history when deciding
what to encode. No direct change to the rememberer.

### 11.7 With approvals

**Visibility benefit.** Approval-related events
(`evt.action.approval.requested/approved/denied/expired`) are in the
whitelist. The next-turn agent sees that an approval was requested,
was acted on, or has expired — useful state.

### 11.8 With idempotency (after Phase 5)

`turn_idempotency_keys` becomes global (no `session_id`) with a TTL.
Discord-message idempotency persists by `idempotency_key` only. The
TTL replaces the implicit "expires when the session rotates" behaviour
with an explicit time-bound (default 24h).

---

## 12. Rendering — concrete example for the failing case

For the documented case (`trn_01ksh81hrrnz04k8zgkxtx42f6`), the
`recent_external_events` block on the 04:17 wake would have included
approximately the following (truncated for illustration):

```
recent_external_events (last K events you have access to, chronological,
oldest first; loop-trace events are filtered out at the system level).
each line is one event row as JSON: {id, created_at, turn_id, event_type, payload}.
the agent uses canonical IDs in payloads to re-fetch full content via existing
capabilities (email.read, calendar.list, drive.read, etc.).

{"id":"evt_...","created_at":"2026-05-25T22:01:14Z","turn_id":"trn_01ksgjg2h2rsqftsgjdtx73m6v","event_type":"evt.turn.started","payload":{"wake_kind":"provider_sync","user_message":"Provider sync wake: Google Gmail\n\nGoogle Gmail sync found 2 new inbound messages. ..."}}
{"id":"evt_...","created_at":"2026-05-25T22:01:15Z","turn_id":"trn_01ksgjg2h2rsqftsgjdtx73m6v","event_type":"evt.action.execution.succeeded","payload":{"capability_id":"cap.email.read","action_attempt_id":"aat_...","status":"succeeded","execution_output":{"_truncated":true,"message":{"message_id":"19c638912663c9e5","subject":"Reminder: $X balance due","sender":"accounting@junehomes.com"},"_compact":true}}}
{"id":"evt_...","created_at":"2026-05-25T22:01:16Z","turn_id":"trn_01ksgjg2h2rsqftsgjdtx73m6v","event_type":"evt.action.execution.succeeded","payload":{"capability_id":"cap.email.read","action_attempt_id":"aat_...","status":"succeeded","execution_output":{"_truncated":true,"message":{"message_id":"19c63892fa1b4c10","subject":"Re: balance reminder (auto)","sender":"accounting@junehomes.com"},"_compact":true}}}
{"id":"evt_...","created_at":"2026-05-25T22:01:18Z","turn_id":"trn_01ksgjg2h2rsqftsgjdtx73m6v","event_type":"evt.assistant.emitted","payload":{"text":"June Homes just sent an unread balance reminder from accounting@junehomes.com at 21:58, and a second auto-reminder from the same address at 22:00. Both flagged INBOX/UNREAD."}}
{"id":"evt_...","created_at":"2026-05-25T22:01:18Z","turn_id":"trn_01ksgjg2h2rsqftsgjdtx73m6v","event_type":"evt.turn.completed","payload":{"outcome":"message"}}

{"id":"evt_...","created_at":"2026-05-25T22:03:26Z","turn_id":"trn_01ksgjm37evgfxdj8j4ttysf7m","event_type":"evt.turn.started","payload":{"wake_kind":"user_message","user_message":"delete both. also, can we send new june homes to spam or trash or something?"}}
{"id":"evt_...","created_at":"2026-05-25T22:03:55Z","turn_id":"trn_01ksgjm37evgfxdj8j4ttysf7m","event_type":"evt.model.failed","payload":{"failure_reason":"...","failure_code":"model_tool_call_arguments",...}}
{"id":"evt_...","created_at":"2026-05-25T22:03:55Z","turn_id":"trn_01ksgjm37evgfxdj8j4ttysf7m","event_type":"evt.turn.completed","payload":{"outcome":"silent"}}

...

{"id":"evt_...","created_at":"2026-05-26T04:17:47Z","turn_id":"trn_01ksh81hrrnz04k8zgkxtx42f6","event_type":"evt.turn.started","payload":{"wake_kind":"user_message","user_message":"try again (delete both. also, can we send new june homes to spam or trash or something?)"}}
```

The two June Homes `message_id`s are in plain JSON in the `succeeded`
events. "Try again (delete both…)" parses cleanly: the agent reads the
block, picks the two IDs from the 22:01 reads, calls
`email.trash([19c638912663c9e5, 19c63892fa1b4c10], …)` in the first
round, replies in the second.

No curated transcript. No IDs index. No lifecycle state machinery. The
events stream is the truth, and the agent reads it.

---

## 13. Acceptance criteria

The cutover is shippable when ALL hold:

### 13.1 Functional

1. A new integration test
   (`tests/integration/test_recent_events_acceptance.py`) constructs a
   session-equivalent history with: a provider-sync wake that ran
   `cap.email.read` on two messages and ended with a non-silent
   assistant_message, then a subsequent user-message wake. The test
   asserts the user-message wake's initial `ModelRequest` contains a
   `recent_external_events` system part whose content includes both
   `message_id` strings.
2. Same test asserts the prior `evt.assistant.emitted` text is also
   in the block.
3. A unit test confirms `EXTERNAL_EVENT_TYPES` excludes
   `evt.model.started`, `evt.model.completed`, `evt.action.proposed`,
   `evt.action.policy_decided`, `evt.action.execution.started`,
   `evt.agent.value_emitted`.

### 13.2 Compact-view rule

4. Unit tests verify that an oversized payload retains all `*_id` and
   `*_ids` fields verbatim, replaces large list/dict fields with
   `{"_kind", "_size", "_byte_size"}` markers, and sets
   `_truncated: true` at the top level.

### 13.3 Budget reformulation

5. An integration test
   (`tests/integration/test_budget_wrap_up.py`) constructs a loop run
   that crosses `turn_budget_seconds_soft`. Asserts:
   - An `evt.agent.wrap_up_nudged` event is emitted exactly once.
   - The next model call sees `WRAP_UP_NUDGE_TEXT` in its system
     messages.
   - The loop continues running until either the agent emits a
     message or the hard cap is crossed.
   - When the agent emits a message after the nudge, the turn ends
     with `outcome="message"`, not `outcome="budget_exhausted"`.
6. A second integration test crosses the hard cap and asserts the
   loop returns `outcome="budget_exhausted"` with no nudge emission
   on this final call.

### 13.4 Session abolition (Phase 5)

7. After Phase 5: `grep -rn "session_id\|SessionRecord\|active_session\|_rotate_active_session\|_auto_rotation_reason\|auto_rotate_" src/ariel/ --include='*.py'`
   returns zero matches.
8. After Phase 5: the migration runs forward on a fresh DB and on a
   DB populated with the prior schema; both end in the same final
   schema state.
9. After Phase 5: the same integration tests from §13.1 still pass
   (the events-window block doesn't depend on `session_id`).

### 13.5 Documented-failure rerun

10. A scripted replay of the 04:17 failure case completes with:
    - `email.trash` called in the first or second model round
    - Total model rounds ≤ 6
    - Wall-clock ≤ 60 seconds
    - Turn outcome `"message"`, not `"budget_exhausted"` or `"model_failed"`.

### 13.6 Doctrine & code hygiene

11. No new "scorer", "ranker", "router", "classifier", "helper",
    "synthesizer", "planner" module. `grep -rn` for those names
    in new files returns zero.
12. No new agent-facing capability. The capability_registry diff is
    empty on the model-facing surface.
13. `make verify` (full ruff + mypy + pytest) is green at the end of
    every phase.

---

## 14. Key decisions, justified

| Decision | Alternative considered | Why this one |
|---|---|---|
| One events block, no curated transcript or IDs index | Path C from v1 spec (curated transcript + IDs index + lifecycle eviction) | v1 was deterministic judgment dressed up. v2 is content-agnostic. |
| Whitelist by event_type | Include every event_type | The cut is structural ("loop trace" vs "world/conversation event") — the user explicitly endorsed this distinction. |
| Compact-view above 4KB, not whole payloads always | Whole payloads always | One large payload could push out many small recent events. The 4KB threshold mirrors the existing observation cap (`.agency/report.md:437-458`). |
| IDs preserved verbatim in compact view | Replace with pointer marker only | The whole point of compact view is that the agent can re-fetch via existing capabilities. Without IDs, no re-fetch. |
| Re-fetch via existing capabilities, no new syscall | Add `agent.read_event_payload(event_id=…)` | New syscall = new agent-facing surface = new prompt overhead. Existing capabilities already do this. |
| Chronological order, oldest first | Most recent first | Reading order matches transcript reading; the current turn's `evt.turn.started` is naturally the last line, which composes well with the immediately-following user prompt. |
| Soft cap injects nudge once per turn | Repeat nudge every K seconds | Repeated nudges become noise the model learns to ignore. Once-per-turn is honest. |
| Soft 600s / hard 1800s main loop | 300s / 900s, or unbounded | 600s gives a J.A.R.V.I.S.-class agent room for genuine work; 1800s is a circuit breaker, not a budget. |
| Smaller budgets for research/memory loops | Same as main loop | Research is intentionally narrower; memory loops are short, well-scoped. Per-loop sizing is the user-visible reality. |
| Kill sessions as final phase | Kill sessions first | Doing it last lets the events-window ship without depending on the (larger) session refactor. |
| `session_id` columns dropped outright, no aliasing | Keep as nullable for audit | "Hard cutover, no fallbacks." Either we believe sessions are gone or we don't. |
| Whitelist `evt.memory.recalled` but exclude `evt.memory.recall_failed` | Whitelist both | The recalled event captures what was pulled (useful next turn); recall_failed is an absence-of-data signal, recoverable from missing recall_v1 items. |
| Whitelist `evt.run.validation_failed` | Exclude (loop trace) | The agent learning that its prior sandbox program crashed is real signal — it shouldn't repeat the same broken program. |
| Single covering index on `(event_type, created_at)` | Composite index on `(session_id, created_at)` | Phase 5 deletes `session_id`. Designing the index for the post-cutover world avoids a second migration. |

---

## 15. Phased implementation

Six phases. Each is independently shippable and leaves `make verify`
green. No phase introduces a feature flag or fallback.

### Phase 1 — Schema + module skeleton (no behaviour change)
- Migration: add covering index on `events(event_type, created_at)`.
- Add `src/ariel/conversational_continuity.py` with
  `EXTERNAL_EVENT_TYPES`, function signatures (NotImplementedError
  bodies), full unit tests for the eventual rendering.
- `config.py`: add the new settings (events block + soft/hard budget)
  with validators. **Do not** delete the old settings yet.
- Unit tests for `_compact_event_payload` (pure function, easy
  coverage).
- Acceptance: migration runs, mypy/ruff/pytest green.

### Phase 2 — Events block lands in initial context
- Implement `build_recent_events_block` and `_compact_event_payload`.
- Wire into `_build_initial_messages` (`app.py:396`). New
  `SystemPromptPart` ordering: after `recall_v1`, before `open_jobs`.
- Tests: §13.1 (Functional) and §13.2 (Compact-view).
- Acceptance: the events block is in every wake's initial context.
  The old behaviour (no events surfaced) is gone.

### Phase 3 — Budget reformulation
- `LoopConfig` schema change: `budget_seconds_{soft,hard}`,
  `max_model_calls_{soft,hard}`. Delete `budget_seconds`,
  `max_model_calls`.
- Update every caller of `run_agent_loop` (`app.py`, `memory.py`,
  `research_runtime.py`) to pass the four new fields.
- Implement the wrap-up nudge logic in `agent_loop.py` (§7.2). Add
  the `evt.agent.wrap_up_nudged` event_type.
- Delete `main_turn_budget_seconds` and `agent_loop_max_model_calls`
  from `config.py`.
- Tests: §13.3 (budget).
- Acceptance: a soft-cap turn injects nudge and continues; a hard-cap
  turn cold-stops with `budget_exhausted`.

### Phase 4 — Documented-failure rerun green
- Run §13.5 against a staging instance.
- Tune `recent_events_token_budget`,
  `recent_event_payload_byte_cap`,
  `turn_budget_seconds_soft` if needed.
- Acceptance: §13.5 green.

### Phase 5 — Session abolition
- Migration: drop `session_id` columns from all tables, drop
  `sessions` and `session_rotations` tables.
- Delete `_rotate_active_session`, `_auto_rotation_reason`, and every
  "active session" lookup pattern.
- Delete `SessionRecord`, `SessionRotationRecord` models from
  `db.py`.
- Delete `auto_rotate_max_turns`, `auto_rotate_max_age_seconds` from
  `config.py`.
- Update the events-window query to drop the `session_id` filter.
- Update `_runtime_provenance_for_turn` to use a count-based prior-turn
  slice (it already does — just remove the session filter).
- Update `turn_idempotency_keys` to be globally scoped with a TTL.
- Tests: §13.4.
- Acceptance: zero matches for session-related names; full suite
  green; staging Discord DM flow unchanged.

### Phase 6 — Docs and prompt
- Update `docs/jarvis-system-prompt.md` with one paragraph on the new
  block.
- Update `docs/modules/agent-loop.md`, `docs/modules/memory.md`.
- Update `docs/conventions.md` if any session-named conventions linger.
- Acceptance: docs reflect post-cutover reality; doctrine review
  passes (§13.6).

---

## 16. Risks and mitigations

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| Events block bloats a long-running history into noise | medium | low | Token budget evicts oldest first; the 4KB per-event cap keeps any single event from dominating. |
| Soft-cap nudge ignored; model keeps tool-calling past it | medium | medium | Hard cap is the safety net. Wrap-up text is unambiguous ("do not start new long tool chains"). If model ignores in practice, raise the soft cap closer to the hard cap. |
| Wrap-up nudge fires on small turns where it isn't needed | low | low | Defaults (600s / 120 calls soft) are well above typical small-turn sizes (~5s / ~3 calls). |
| Session removal misses a query and orphans rows | medium | high | Phase 5 acceptance test §13.4.7 greps the entire `src/ariel/` for `session_id` / `SessionRecord` / `active_session`. CI gate. |
| Compact-view rule drops a field the agent actually needed | medium | medium | All `*_id`/`*_ids` keys are preserved verbatim. If a specific field name pattern needs preservation, add it to the regex — code change, no doctrine reopen. |
| New covering index slow on huge events tables | low | low | Single index, single column subset. Migration runs in seconds on production-scale data per Postgres benchmarks. |
| Idempotency window shrinks at session-abolition phase | low | low | TTL replacement is explicit; default 24h covers all realistic windows. |
| Backfill: the events-window starts empty for sessions created before cutover | n/a | n/a | Not a problem: the events table already has historical events; the cutover changes only how they're read into context. No backfill needed. |

---

## 17. Out of scope (explicit), with links forward

- **Cross-history compaction.** Still the rememberer's job. The
  events-window provides raw recency; the rememberer provides
  consolidated long-term notes via `memory.remember`.
- **Long-horizon memory** (multi-week / multi-month). Same answer:
  `memory.search` / `memory.recall`.
- **Spreading-activation / association graphs.** Out, per crystallization
  doctrine. Future spec if ever.
- **A general-purpose `agent.read_event(event_id=…)` syscall.** Not
  needed — existing capabilities re-fetch by canonical ID.
- **Per-event signal scoring or ranking.** Forbidden. Future you should
  flag any PR that introduces it.
- **Cross-actor visibility / multi-user Ariel deployments.** Not in
  scope; one principal per deployment is still the assumed model.

---

## 18. References

- `docs/ai-first.md` — code owns no judgment; crystallization
  doctrine.
- `docs/memory-cognition-research.md` — the prior research pass that
  opened this question.
- `docs/modules/agent-loop.md` — agent loop, eviction, emit_value
  semantics.
- `docs/modules/memory.md` — retriever / rememberer / memory layers.
- `docs/jarvis-system-prompt.md` — agent framing.
- Anthropic, "Effective context engineering for AI agents", 2025.
- Microsoft Bot Framework, "Proactive messages with conversation
  references".
- Survey artifacts from this design phase (5 parallel surveys, May
  2026).
