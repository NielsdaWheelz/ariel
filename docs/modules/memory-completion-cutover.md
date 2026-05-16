# Memory Completion Cutover

## Role

This document is the hard-cutover implementation spec that finishes Ariel's
memory system: it closes every gap between the current implementation and
[`memory.md`](memory.md) (the North Star), fixes the correctness defects found
in audit, and surpasses the North Star on retrieval, typing, and evaluation.

This document supersedes `docs/modules/memory-sota-completion-plan.md`. That file
is deleted as the first step of this cutover. Where this document and
[`memory.md`](memory.md) differ, this document wins; the [Amendments To
memory.md](#amendments-to-memorymd) section lists the precise design-doc edits
to apply.

**Status (2026-05-16): the cutover is implemented, verified, and merged to
`main`** — all ten workstreams, migration `20260515_0030`, and `make verify`
green (ruff, mypy, and the full 669-test suite). Everything through the
"Amendments To memory.md" section describes the executed design. The two
sections appended after it — "State Of The Art Positioning" and "Forward
Opportunities And v2 Roadmap" — record how the implementation compares to the
2026 field and define the next iteration; they are roadmap, not part of the
completed cutover.

This is not a polish pass. It changes the schema, deletes two policy functions,
rewrites candidate retrieval, and adds a deterministic predicate registry.

## Current State And Audit Verdict

The memory subsystem is large and ~70% complete: `memory.py` is 6.4k lines, with
~30 canonical and projection tables, a 33-route HTTP API, 28 model capabilities,
async projection workers, and ~2.4k lines of integration tests. The
evidence -> candidate -> review -> active lifecycle, session memory modes, and
privacy-delete/redact/never-remember are real and trustworthy.

It is not finished, and it has correctness defects. Confirmed by audit:

1. **Conflict cardinality is AI-controlled.** `is_multi_valued` is a per-row
   boolean supplied by the extraction model per candidate (`memory.py:1175`,
   `propose_memory_candidate`). A candidate the model marks multi-valued evades
   both conflict detection and supersession, leaving two active assertions for
   one single-valued predicate. The partial unique index
   `ix_memory_assertions_single_active_unique` does not catch it.
2. **Conflict sets never close on lifecycle change.** `correct_assertion`,
   `retract_assertion`, `delete_assertion`, `privacy_delete_assertion`,
   `mark_assertion_stale`, and `reject_candidate` never settle an open conflict
   set referencing the affected assertion. Conflicts orphan permanently and
   recall keeps emitting "unresolved memory conflicts exist".
3. **`ignored` conflict state is unreachable.**
4. **Scope-bound memory modes are bypassed.** The chat-turn write path is gated
   by a raw `active_session.memory_mode == "normal"` compare (`app.py:7320`),
   not policy resolution, so thread/project/repo/user `no_memory` bindings are
   ignored for evidence, action-trace, and extraction writes. `build_memory_context`
   skips the mode check entirely when `current_session_id is None`. Proactive
   `remember` ignores `proactive_case` bindings.
5. **Scope binding is not real policy.** Mode precedence
   (`no_memory > temporary > normal`) is unimplemented; `set_memory_scope_binding`
   never sets `expires_at` so expiry is dead code; binding changes emit no event
   and no version record.
6. **Retrieval is not hybrid.** "Keyword" is naive Python set-intersection with
   no Postgres full-text. The symbol projection is queried but never written
   (dead code). The graph projection is written but never read. Salience, user
   priority, source trust, temporal validity, sensitivity, and topic membership
   are display-only and never rank or filter. Reasoning traces and negative
   memory are never retrieved. Episodes, procedures, action traces, and project
   state are gathered recency-only with zero query relevance.
7. **Consolidation has no autonomous trigger.** The only `MemoryProjectionJobRecord`
   enqueue site (`memory.py:1037`) hardcodes `projection_kind="embedding"`. No
   code enqueues `context_block`/`hot_index`/`topic_block`/`project_state`/`export`
   jobs, so `process_memory_maintenance_job` is dead in production.
8. **Action traces are incomplete.** Proactive action execution emits no traces;
   the only creation site is the chat-turn path.
9. **Hot index has no token budget.** `memory.md` mandates 1,500/2,500; nothing
   enforces it.
10. **Memory mutation events are dropped.** HTTP/capability/worker mutation paths
    discard the `evt.memory.*` event lists the memory functions return.
    `EventRecord` requires a `turn_id`, so events are only persisted for
    turn-driven writes.
11. **No long-memory eval suite.** The eval harness is real but ships zero of the
    mandated adversarial cases; no test fails under vector-only or keyword-only
    retrieval.

## Goals

- Close every gap between the implementation and [`memory.md`](memory.md).
- Eliminate the conflict-lifecycle correctness defects: the active fact store
  can never hold two contradictory single-valued assertions, and conflict sets
  always reach a terminal state.
- Make memory modes a real scoped policy with deterministic precedence, expiry,
  and audit. "Do not remember this project/thread" is honored everywhere.
- Make candidate retrieval genuinely hybrid and deterministic: a fused
  multi-signal candidate pool that no single signal can satisfy alone.
- Make every memory kind retrievable: semantic assertions, episodes, reasoning
  traces, action traces, procedures, project state, negative memory, hot index,
  topic blocks, conflicts.
- Make consolidation and projection rebuilds autonomously triggered and
  observable.
- Persist every memory mutation as a durable, queryable event regardless of
  entry path.
- Ship a long-memory evaluation suite that gates the build.

### Surpass Goals

Beyond the North Star:

- **Deterministic predicate registry.** Predicate cardinality, conflict policy,
  value kind, sensitivity default, and confidence decay are declared in code,
  not inferred per candidate by a model. This is the structural fix for the
  conflict defect and a SOTA typed-memory schema.
- **Reciprocal Rank Fusion retrieval.** Multiple ranked retrieval signals fuse
  into one deterministic candidate pool with a full per-candidate feature
  vector, replacing the current "union of ad hoc matches".
- **First-class negative memory and reasoning-trace recall.** Negative knowledge
  is a typed assertion family; reasoning traces are retrieved and consolidated
  into procedures and negative memory.
- **Confidence decay.** Each predicate declares a half-life; effective confidence
  is a recall feature and a deterministic staleness trigger.
- **Eval as a regression gate.** The long-memory suite runs in `make verify`
  with adversarial cases that fail under degenerate retrieval.

## Non-Goals

- No provider-hosted memory, vector DB, graph DB, or markdown directory as
  canonical state. PostgreSQL stays canonical.
- No learned reranker or any deterministic component that decides final
  relevance. Reciprocal Rank Fusion produces the bounded candidate *pool*; AI
  curation owns relevance. This boundary follows [`../ai-first.md`](../ai-first.md).
- No external memory product (Zep, Mem0, Letta, Graphiti, Neo4j) as a runtime
  dependency. They remain reference models.
- No backward compatibility: no legacy response shapes, no compatibility shims,
  no dual-write, no fallback recall path.
- No model fine-tuning, no prompt cache as memory, no transcript replay.
- No new memory submodules or facade layers unless `memory.py` reaches a
  concrete complexity problem a split removes. Predicate registry and policy
  resolution live in `memory.py` alongside existing memory constants.
- No multi-agent memory orchestration. Extraction and curation remain bounded
  single model calls.

## Cutover Rules

- This is a hard cutover. No intermediate state is production-shippable while
  old and new memory behavior are both reachable. Implementation may be split
  into reviewable commits.
- Delete `docs/modules/memory-sota-completion-plan.md`.
- Delete `session_allows_memory_operation` and `scope_allows_memory_operation`.
  They are replaced by one `resolve_memory_policy` function. No call site keeps
  the old functions.
- Delete the `is_multi_valued` parameter from `propose_memory_candidate`, from
  the `POST /v1/memory/candidates` request contract, and from the
  `cap.memory.propose` input schema. Cardinality is derived from the predicate
  registry only.
- Delete the naive Python keyword-overlap retrieval block in
  `build_memory_context`. Lexical retrieval is Postgres full-text only.
- Delete the `current_session_id is None` unrestricted-recall branch in
  `build_memory_context`. Recall is never unrestricted.
- No memory `projection_kind` may exist in a schema CHECK constraint without a
  code path that both enqueues and consumes it.
- No memory mutation function may return an event list that any caller silently
  discards.
- There are no production memory rows (the deployment is fresh; the worker and
  Discord bot have not started). The cutover migration needs no data backfill
  and may drop and recreate freely.
- `make verify` (ruff, ruff format, mypy strict, pytest) and the long-memory
  eval suite must pass before the cutover is complete.

## Target Behavior

### User Experience

Unchanged in surface, correct underneath. The user talks to Ariel normally
("remember this", "forget that", "what do you recall", "never remember this
thread", "why do you think that"). What changes:

- A contradiction between single-valued facts always opens exactly one conflict
  and is surfaced as uncertainty until resolved. The store never silently holds
  both. Resolving or invalidating either side always closes the conflict.
- "Do not remember this project / thread / case" is honored for recall,
  extraction, proactive writes, action traces, and consolidation — not only for
  the current session.
- Recall quality is hybrid: a fact relevant by entity, graph, or temporal
  validity is retrieved even when it shares no keywords or embedding proximity
  with the query.
- Ariel recalls negative knowledge ("you already rejected approach X", "that
  file was already checked") and reasoning traces, not only positive facts.
- Every memory change — from chat, HTTP, a model capability, the worker, or
  proactive deliberation — is in the queryable memory event log.

### Model Behavior

The model handles memory through the existing 28 `cap.memory.*` capabilities,
reached via the single `run` tool. Two contract changes:

- `cap.memory.propose` no longer accepts `is_multi_valued`.
- `cap.memory.set_scope_mode` accepts an optional `expires_at`.

### Failure Behavior

- Recall fails closed: when policy requires memory and it cannot be assembled,
  it returns a typed empty context with an auditable reason, never unrestricted
  or stale recall.
- A predicate the extractor emits that is not in the registry resolves to the
  default spec (single-valued, conflict policy) — fail-safe toward detecting
  contradictions rather than missing them.
- Projection job failure is observable via `projection_health` and never
  corrupts canonical state.

## Architecture And Final State

The eight-layer architecture of [`memory.md`](memory.md) stands. This cutover
corrects and completes four layers and adds one structure.

### New Structure: Predicate Registry

A deterministic, code-owned registry declares every memory predicate's type
shape. It is a closed vocabulary the deterministic rails own — analogous to
`capability_registry.py`. It is the single source of truth for:

- **cardinality** — single-valued or multi-valued (drives the conflict and
  supersession decision; replaces the AI-supplied `is_multi_valued`).
- **resolution policy** — `conflict`, `supersede`, or `coexist`.
- **value kind** — `text`, `enum`, `date`, `datetime`, `number`, `json` (drives
  deterministic value validation).
- **sensitivity default** — the default `MemorySensitivityLabelRecord` label.
- **decay half-life** — drives effective-confidence computation and staleness.

The model still authors predicate *strings* and *values* (AI owns extraction
judgment). The registry deterministically resolves *type behavior* from the
predicate. This is the [`../ai-first.md`](../ai-first.md) split: judgment is the
model's, schema is the rails'.

### Corrected Layer: Review And Conflict Control Plane

Conflict opening, settlement, and closure become total. Every assertion
lifecycle transition that touches a conflict member re-evaluates the set. A
conflict set always reaches `open -> resolved` or `open -> ignored`. The
`conflicted` lifecycle state always implies membership in an `open` set.

### Corrected Layer: Policy And Access

One `resolve_memory_policy` function resolves the effective memory mode for any
operation from the full scope chain (session, thread, proactive case, repo,
project, user), with deterministic severity precedence and expiry. Every memory
operation path — chat turn, proactive deliberation, background extraction,
search, recall diagnostics, action-trace writes, consolidation — calls it.

### Corrected Layer: Candidate Retrieval Service

Candidate retrieval becomes a deterministic hybrid pipeline: each signal emits a
ranked list, Reciprocal Rank Fusion fuses them into one bounded pool, rails
filter the pool, and every candidate carries a full feature vector. AI curation
receives the pool and accounts for every candidate. Retrieval covers all memory
kinds, not only semantic assertions.

### Corrected Layer: Projection Layer

Every `projection_kind` is enqueued and consumed. Lexical retrieval uses a real
Postgres `tsvector`. Graph and symbol projections are written and read.
Consolidation and hot-index rebuilds are enqueued by autonomous triggers.

### New Structure: Unified Memory Event Log

A non-turn-scoped append-only `memory_events` table is the single memory event
stream. Every memory mutation, from every entry path, appends to it. The
turn-scoped `EventRecord` stream no longer carries `evt.memory.*` lifecycle
events.

## How This Surpasses The North Star

[`memory.md`](memory.md) requires "hybrid retrieval" and a "type registry" but
does not specify either. This cutover specifies and exceeds them:

- The North Star says multi-valued predicates "must declare that they are
  multi-valued in the schema or type registry" but no registry exists. This
  cutover builds the full predicate registry and makes cardinality, conflict
  policy, value kind, sensitivity, and decay all registry-driven.
- The North Star lists ~24 retrieval signals as a flat set. This cutover
  specifies *how* they combine: Reciprocal Rank Fusion with a fixed `k`, stable
  tie-breaking, and a per-candidate feature vector — deterministic and
  explainable.
- The North Star lists "negative memory records or typed assertions" as an
  option. This cutover makes negative memory a first-class assertion family with
  its own predicates, retrieval kind, and consolidation path.
- The North Star mentions "verification age" as a signal. This cutover makes
  confidence decay a declared per-predicate half-life feeding both recall
  features and the staleness trigger.
- The North Star requires an eval suite. This cutover makes it a `make verify`
  gate with adversarial cases that fail under degenerate retrieval.

## Data Model Changes

One Alembic migration, `20260515_0030_memory_completion_cutover.py`,
`down_revision = "0029"`. There is no production memory data; the migration
performs schema changes only.

### New Table: `memory_events`

```python
class MemoryEventRecord(Base):
    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # prefix "mze"
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_path: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("turns.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "entry_path IN ('turn', 'http', 'capability', 'worker', "
            "'proactive', 'consolidation')",
            name="ck_memory_event_entry_path",
        ),
        CheckConstraint(
            "event_type LIKE 'evt.memory.%'",
            name="ck_memory_event_type_prefix",
        ),
    )
```

### Schema Edits To Existing Tables

- `memory_assertions`: `ck_memory_assertion_type` adds `'negative'`. `is_multi_valued`
  is retained (the partial unique index needs it) but is always written from the
  predicate registry, never from input.
- `memory_conflict_sets`: add `conflict_type: Mapped[str]` with
  `ck_memory_conflict_set_type` IN `('value_contradiction', 'staleness',
  'scope_overlap')`. `lifecycle_state` already allows `open/resolved/ignored`.
- `memory_scope_bindings`: `ck_memory_scope_binding_scope_type` drops `'session'`.
  Session mode lives only on `SessionRecord.memory_mode`. The bindings table
  covers `user/project/repo/thread/proactive_case`.
- `memory_versions`: `ck_memory_version_canonical_table` adds
  `'memory_scope_bindings'`, `'memory_salience'`, `'memory_conflict_sets'`,
  `'memory_events'`.
- `memory_keyword_projections`: add `search_document: Mapped[str]` (`Text`) and
  `search_vector` as a persisted generated column
  `Computed("to_tsvector('english', search_document)", persisted=True)` typed
  `TSVECTOR`, with a GIN index `ix_memory_keyword_projections_search_vector`.
  Drop the `weighted_terms` column. `canonical_table` CHECK widens to
  `memory_assertions/memory_episodes/memory_reasoning_traces/memory_action_traces/memory_procedures`.
- `memory_projection_jobs`: `ck_memory_projection_job_kind` is reconciled to the
  exact enqueued-and-consumed set:
  `('embedding', 'graph', 'context_block', 'project_state', 'hot_index',
  'topic_block', 'export')`. `keyword`, `entity`, `temporal`, `symbol`, and
  `action_trace` are removed — keyword/entity/temporal/symbol projections are
  written synchronously on activation (see WS-8), and `action_trace` is not a
  projection.
- `pgvector`: confirm the HNSW index on `memory_embedding_projections.embedding`
  exists; add it if absent.

## Workstreams

Ten workstreams. WS-1 is foundational and lands first. WS-10 lands last and
gates the cutover.

---

### WS-1 — Predicate And Type Registry

**Problem.** Cardinality is AI-controlled per candidate. Conflict detection,
supersession, and the single-active invariant all depend on it. The model can
disagree with itself across two candidates for the same predicate.

**Target.** Cardinality and conflict behavior are a deterministic function of
the predicate, declared in a code-owned registry. The candidate input no longer
carries cardinality.

**Design.** Add to `memory.py`, beside the existing constants (`memory.py:52`):

```python
@dataclass(frozen=True, slots=True)
class PredicateSpec:
    predicate: str
    assertion_type: str
    resolution_policy: str        # "conflict" | "supersede" | "coexist"
    value_kind: str               # "text" | "enum" | "date" | "datetime" | "number" | "json"
    sensitivity_default: str      # MemorySensitivityLabelRecord.label value
    decay_half_life_days: float | None
    enum_values: tuple[str, ...] = ()
    description: str = ""

    @property
    def is_multi_valued(self) -> bool:
        return self.resolution_policy == "coexist"


_DEFAULT_PREDICATE_SPEC = PredicateSpec(
    predicate="*",
    assertion_type="fact",
    resolution_policy="conflict",   # unknown predicates are single-valued: fail safe
    value_kind="text",
    sensitivity_default="personal",
    decay_half_life_days=None,
)

_PREDICATE_REGISTRY: dict[str, PredicateSpec] = {
    "profile.display_name": PredicateSpec(
        "profile.display_name", "profile", "supersede", "text", "personal", None),
    "profile.role": PredicateSpec(
        "profile.role", "profile", "supersede", "text", "personal", 365.0),
    "profile.timezone": PredicateSpec(
        "profile.timezone", "profile", "supersede", "text", "personal", None),
    "preference.response_verbosity": PredicateSpec(
        "preference.response_verbosity", "preference", "conflict", "enum",
        "personal", None, enum_values=("terse", "normal", "detailed")),
    "preference.communication_style": PredicateSpec(
        "preference.communication_style", "preference", "conflict", "text",
        "personal", None),
    "preference.code_style": PredicateSpec(
        "preference.code_style", "preference", "coexist", "text", "public", None),
    "project.deadline": PredicateSpec(
        "project.deadline", "project_state", "conflict", "datetime", "personal", None),
    "project.status": PredicateSpec(
        "project.status", "project_state", "supersede", "enum", "personal", 21.0,
        enum_values=("planned", "active", "blocked", "shipped", "abandoned")),
    "project.open_question": PredicateSpec(
        "project.open_question", "project_state", "coexist", "text", "personal", 60.0),
    "project.risk": PredicateSpec(
        "project.risk", "project_state", "coexist", "text", "personal", 60.0),
    "commitment.todo": PredicateSpec(
        "commitment.todo", "commitment", "coexist", "text", "personal", None),
    "decision.architecture": PredicateSpec(
        "decision.architecture", "decision", "coexist", "text", "public", None),
    "repo.convention": PredicateSpec(
        "repo.convention", "repo", "coexist", "text", "public", None),
    "negative.rejected_approach": PredicateSpec(
        "negative.rejected_approach", "negative", "coexist", "text", "public", 120.0),
    "negative.invalid_assumption": PredicateSpec(
        "negative.invalid_assumption", "negative", "coexist", "text", "public", 120.0),
    "negative.already_checked": PredicateSpec(
        "negative.already_checked", "negative", "coexist", "text", "public", 30.0),
    "negative.unsafe_operation": PredicateSpec(
        "negative.unsafe_operation", "negative", "coexist", "text", "public", None),
    # ... full registry is ~50 entries across all nine assertion types.
}


def resolve_predicate_spec(predicate: str) -> PredicateSpec:
    return _PREDICATE_REGISTRY.get(_memory_key(predicate), _DEFAULT_PREDICATE_SPEC)
```

`propose_memory_candidate` loses its `is_multi_valued` parameter and computes
`spec = resolve_predicate_spec(predicate)`; it sets
`assertion.is_multi_valued = spec.is_multi_valued` and validates `value` against
`spec.value_kind` (a typed `E_MEMORY_VALUE_KIND` failure when it does not
parse). Conflict opening uses `spec.resolution_policy` (see WS-2). The extraction
prompt (`memory-extraction-v2`) is given the registry's predicate vocabulary and
asked to prefer registered predicates.

**Files.** `memory.py` (registry, `resolve_predicate_spec`,
`propose_memory_candidate`, extraction prompt), `app.py` and
`response_contracts.py` (`MemoryCandidateRequest` drops `is_multi_valued`),
`capability_registry.py` (`_validate_memory_propose_input` drops the field).

**Acceptance.**
- `is_multi_valued` appears in no input contract; a grep of `src` confirms it is
  only ever assigned from `resolve_predicate_spec(...).is_multi_valued`.
- A test proves two candidates with the same predicate always resolve to the
  same cardinality regardless of any input.
- A test proves an unknown predicate resolves single-valued/`conflict`.
- A test proves a value that violates the predicate's `value_kind` is rejected
  with `E_MEMORY_VALUE_KIND`.

---

### WS-2 — Conflict Lifecycle Correctness And Closure

**Problem.** Cardinality evasion (fixed by WS-1) plus no conflict-set teardown:
conflicts orphan permanently, `ignored` is unreachable, and losing a previously
active fact discards its supersession history.

**Target.** Conflict opening, settlement, and closure are total. A `conflicted`
assertion always belongs to exactly one `open` set. Every lifecycle transition
on a conflict member re-settles its sets. Resolution preserves history.

**Design.**

Conflict opening (`propose_memory_candidate` / `_open_conflict`): a conflict
opens only when `resolve_predicate_spec(predicate).resolution_policy == "conflict"`
and an active single-valued assertion exists for the same subject/predicate/scope.
`supersede` predicates open no conflict — the new candidate supersedes on
activation. `coexist` predicates open no conflict. The conflict set gets
`conflict_type="value_contradiction"`.

New helper, called at the end of `correct_assertion`, `retract_assertion`,
`delete_assertion`, `privacy_delete_assertion`, `mark_assertion_stale`,
`reject_candidate`, and `resolve_conflict`:

```python
def _settle_conflict_sets_for_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Re-evaluate every open conflict set that the assertion belongs to and
    close any that no longer has a live contradiction. Returns event dicts."""
    events: list[dict[str, Any]] = []
    set_ids = db.scalars(
        select(MemoryConflictMemberRecord.conflict_set_id).where(
            MemoryConflictMemberRecord.assertion_id == assertion_id
        )
    ).all()
    for set_id in set_ids:
        conflict = db.get(MemoryConflictSetRecord, set_id)
        if conflict is None or conflict.lifecycle_state != "open":
            continue
        member_ids = db.scalars(
            select(MemoryConflictMemberRecord.assertion_id).where(
                MemoryConflictMemberRecord.conflict_set_id == set_id
            )
        ).all()
        live = [
            a for a in (db.get(MemoryAssertionRecord, mid) for mid in member_ids)
            if a is not None and a.lifecycle_state in {"active", "candidate", "conflicted"}
        ]
        if len(live) >= 2:
            continue                       # contradiction still live: stays open
        if len(live) == 1:
            winner = live[0]
            conflict.lifecycle_state = "resolved"
            conflict.resolution_assertion_id = winner.id
            if winner.lifecycle_state != "active":
                events.extend(_activate_assertion(
                    db, assertion=winner, actor_id=actor_id, now=now, new_id_fn=new_id_fn))
        else:                              # contradiction evaporated entirely
            conflict.lifecycle_state = "ignored"
        conflict.updated_at = now
        _record_version(
            db, table="memory_conflict_sets", record_id=conflict.id,
            change_type="reviewed", actor_id=actor_id,
            reason="conflict settled by member lifecycle change",
            now=now, new_id_fn=new_id_fn)
        events.append({
            "event_type": "evt.memory.conflict_resolved",
            "payload": {"conflict_set_id": conflict.id,
                        "lifecycle_state": conflict.lifecycle_state,
                        "resolution_assertion_id": conflict.resolution_assertion_id},
        })
    return events
```

`resolve_conflict` loser handling becomes policy-driven: a losing member that
was `active` becomes `superseded` with `superseded_by_assertion_id` set to the
winner (history preserved); a losing `candidate`/`conflicted` member becomes
`rejected`. The current unconditional `rejected` + cleared `superseded_by` is
deleted.

Invariants enforced: `conflicted` is only ever set inside `_open_conflict`
(which always inserts membership); any path that moves an assertion out of
`{active, candidate, conflicted}` calls `_settle_conflict_sets_for_assertion`.
`mark_assertion_stale` requires a non-empty `reason` (typed
`E_MEMORY_STALE_REASON_REQUIRED`) and records it in the version row, satisfying
"a stale assertion must identify staleness rationale".

**Files.** `memory.py` (`_open_conflict`, `resolve_conflict`,
`_settle_conflict_sets_for_assertion`, the six mutation functions,
`mark_assertion_stale`).

**Acceptance.**
- Rewrite `test_rejected_conflict_member_cannot_be_reactivated_by_resolution`:
  rejecting a conflicted member settles the conflict toward the surviving active
  assertion (`resolved`), not leaves it open.
- Tests: conflict open; resolution with a non-member id rejected (the membership
  guard, currently untested); valid resolution preserves winner version history
  and supersedes a previously-active loser; correcting/retracting/deleting a
  conflict member closes the set; a conflict whose every member is invalidated
  reaches `ignored`.
- A test calls `build_memory_context` while a conflict is open and asserts the
  fact is surfaced as a conflict, never as settled.
- No code path leaves a `conflicted` assertion outside an `open` set.

---

### WS-3 — Memory Mode And Scope Binding Policy

**Problem.** Two policy functions with bypasses; no precedence; expiry is dead;
binding changes are unaudited; the chat-turn write path ignores scope bindings.

**Target.** One `resolve_memory_policy`. Effective mode is the strictest mode in
the scope chain. Expiry works. Every binding change is audited. Every operation
path resolves policy the same way.

**Design.**

```python
@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    allowed: bool
    operation: str                 # "recall" | "extract" | "write" | "consolidate"
    effective_mode: str            # "normal" | "temporary" | "no_memory"
    controlling_scope_type: str
    controlling_scope_key: str
    controlling_binding_id: str | None
    reason: str
    considered_scopes: tuple[dict[str, Any], ...]


_MODE_SEVERITY = {"normal": 0, "temporary": 1, "no_memory": 2}
_SCOPE_SPECIFICITY = {                       # lower = more specific
    "thread": 10, "proactive_case": 20, "repo": 30, "project": 40, "user": 50,
}


def resolve_memory_policy(
    db: Session,
    *,
    operation: str,
    now: datetime,
    session_id: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    project_key: str | None = None,
    repo_key: str | None = None,
    actor_id: str | None = None,
) -> MemoryPolicyDecision:
    """Resolve the effective memory mode for an operation across the full scope
    chain. The strictest mode wins; the most specific scope carrying that mode
    is reported as controlling. Expired bindings are ignored."""
    considered: list[dict[str, Any]] = []

    # Session scope: mode lives on SessionRecord, not in the bindings table.
    if session_id is not None:
        session = db.get(SessionRecord, session_id)
        if session is None:
            return MemoryPolicyDecision(
                False, operation, "no_memory", "session", session_id, None,
                "session not found", ())
        considered.append({"scope_type": "session", "scope_key": session_id,
                            "specificity": 0, "memory_mode": session.memory_mode,
                            "binding_id": None})

    # The other five scope types come from memory_scope_bindings.
    wanted: list[tuple[str, str | None]] = [
        ("thread", thread_id), ("proactive_case", proactive_case_id),
        ("repo", repo_key), ("project", project_key), ("user", USER_SUBJECT_KEY),
    ]
    for scope_type, scope_key in wanted:
        if scope_key is None:
            continue
        binding = db.scalar(
            select(MemoryScopeBindingRecord)
            .where(
                MemoryScopeBindingRecord.scope_type == scope_type,
                MemoryScopeBindingRecord.scope_key == scope_key,
                or_(MemoryScopeBindingRecord.expires_at.is_(None),
                    MemoryScopeBindingRecord.expires_at > now),
            )
            .order_by(MemoryScopeBindingRecord.updated_at.desc())
            .limit(1)
        )
        if binding is not None:
            considered.append({
                "scope_type": scope_type, "scope_key": scope_key,
                "specificity": _SCOPE_SPECIFICITY[scope_type],
                "memory_mode": binding.memory_mode, "binding_id": binding.id})

    if not considered:
        return MemoryPolicyDecision(
            True, operation, "normal", "default", "default", None,
            "no binding applies", ())

    # Strictest mode wins; the most specific scope carrying it is controlling.
    strictest = max(_MODE_SEVERITY[s["memory_mode"]] for s in considered)
    carriers = [s for s in considered if _MODE_SEVERITY[s["memory_mode"]] == strictest]
    controlling = min(carriers, key=lambda s: s["specificity"])
    mode = controlling["memory_mode"]
    return MemoryPolicyDecision(
        allowed=(mode == "normal"),
        operation=operation,
        effective_mode=mode,
        controlling_scope_type=controlling["scope_type"],
        controlling_scope_key=controlling["scope_key"],
        controlling_binding_id=controlling["binding_id"],
        reason=f"effective mode {mode} from {controlling['scope_type']} scope",
        considered_scopes=tuple(considered),
    )
```

`session_allows_memory_operation` and `scope_allows_memory_operation` are
deleted. Every call site moves to `resolve_memory_policy`. The chat-turn write
block at `app.py:7320` changes from `if active_session.memory_mode == "normal":`
to resolving policy for the session/thread/project/repo/user chain and gating on
`policy.allowed`. `build_memory_context` always resolves policy; the
`current_session_id is None` branch is deleted — when no session is supplied it
resolves the project/repo/user chain. Proactive deliberation and proactive
`remember` pass `proactive_case_id` explicitly.

`set_memory_scope_binding` gains an `expires_at: datetime | None` parameter,
writes a `MemoryVersionRecord` (`canonical_table="memory_scope_bindings"`), and
returns an `evt.memory.scope_binding_changed` event that callers persist via the
WS-6 event helper. `PUT /v1/memory/scope-bindings` and `cap.memory.set_scope_mode`
accept `expires_at`.

**Files.** `memory.py` (`resolve_memory_policy`, delete the two old functions,
`set_memory_scope_binding`, `build_memory_context`, `propose_memory_candidate`,
`record_turn_memory_evidence`, `consolidate_memory`, extraction worker),
`app.py` (chat-turn block, memory HTTP handlers), `proactivity.py` (both call
sites), `capability_registry.py`, `worker.py` (consolidation enqueue gate).

**Acceptance.**
- `session_allows_memory_operation` and `scope_allows_memory_operation` do not
  exist; grep confirms.
- Tests: a `project` `no_memory` binding blocks recall, extraction, proactive
  writes, action-trace writes, and consolidation; a `thread` `no_memory` binding
  blocks the chat-turn write path even when `SessionRecord.memory_mode` is
  `normal`; a broad `no_memory` is not overridden by a narrower `normal`
  (strictest wins); an expired binding stops applying; a binding change writes a
  `memory_events` row and a `MemoryVersionRecord`.
- Recall diagnostics report `controlling_scope_type` and `controlling_binding_id`.

---

### WS-4 — Hybrid Retrieval And Complete Candidate Kinds

**Problem.** Retrieval is a union of ad hoc matches; "keyword" is naive Python;
symbol is dead; graph is unread; most signals are display-only; reasoning traces
and negative memory are never retrieved; non-assertion kinds are recency-only.

**Target.** A deterministic Reciprocal Rank Fusion pipeline over real signals,
producing one bounded candidate pool covering every memory kind, each candidate
carrying a full feature vector. No single signal can satisfy the acceptance
suite alone.

**Design.** `build_memory_context` candidate retrieval is rewritten as:

1. **Resolve policy** (WS-3). Fail closed to a typed empty context if not
   allowed.
2. **Run each signal**, each producing a ranked list of `(canonical_table,
   canonical_id)` best-first:
   - `vector` — pgvector cosine distance over `memory_embedding_projections`,
     bounded by a distance ceiling (`ARIEL_memory_vector_distance_ceiling`,
     default `0.6`).
   - `lexical` — Postgres full-text: `plainto_tsquery('english', :q)` against
     `memory_keyword_projections.search_vector`, ranked by `ts_rank_cd`.
   - `entity` — query terms matched to `memory_entities`, then
     `memory_entity_projections`.
   - `graph` — entities reachable within depth 3 via `memory_graph_projections`
     (WS-8 makes this a real multi-hop projection), ranked by hop distance.
   - `symbol` — repo-scoped identifier/path tokens via
     `memory_symbol_projections` (WS-8 makes this written).
   - `temporal` — assertions/episodes whose validity interval contains `now` or
     whose `occurred_at` is near the query's referenced time, via
     `memory_temporal_projections`.
   - `recency` — most-recently-updated active rows per kind, as a baseline
     signal so every kind is represented.
3. **Fuse** with Reciprocal Rank Fusion:

```python
def _fuse_candidates(
    signal_rankings: Mapping[str, Sequence[tuple[str, str]]],
    *,
    k: int = 60,
) -> list[tuple[tuple[str, str], float, dict[str, int]]]:
    """Reciprocal Rank Fusion. Returns (canonical_ref, fused_score, per-signal
    rank) sorted by score desc then ref asc. Deterministic for fixed input."""
    scores: dict[tuple[str, str], float] = {}
    ranks: dict[tuple[str, str], dict[str, int]] = {}
    for signal, ranking in sorted(signal_rankings.items()):
        for rank, ref in enumerate(ranking):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (k + rank + 1)
            ranks.setdefault(ref, {})[signal] = rank + 1
    return sorted(
        ((ref, scores[ref], ranks[ref]) for ref in scores),
        key=lambda item: (-item[1], item[0]),
    )
```

4. **Apply rails** to the fused list: lifecycle (`active` only), policy/scope,
   sensitivity label, retention, trust boundary. Each excluded candidate is
   recorded with a deterministic omission reason.
5. **Build the feature vector** for each surviving candidate:
   `rrf_score`, `signal_ranks` (per-signal rank), `vector_distance`,
   `lexical_rank`, `salience_score`, `user_priority`, `source_trust`,
   `effective_confidence` (see below), `verification_age_days`, `conflict_status`,
   `validity` (`valid_from`/`valid_to`), `topic_membership`.
6. **Cap** to `candidate_limit` by `rrf_score` and hand the pool to AI curation.

`effective_confidence` is deterministic:
`confidence * 0.5 ** (age_days / spec.decay_half_life_days)` when the predicate
declares a half-life, else `confidence`. It is a recall feature; it does not
filter.

Curation (`memory-curation-v2`) receives the full pool and must account for
every candidate as selected or omitted. The naive keyword block and the
`current_session_id is None` branch are deleted.

The returned `memory_context` (schema `memory.sota.v2`) adds `reasoning_traces`
and `negative_memory` sections; `recall_window.candidate_memories[*]` carries the
feature vector; `projection_health` reports `failed_projection_jobs`,
`dead_letter_projection_jobs`, and `stale_projection_count`.

**Files.** `memory.py` (`build_memory_context`, `_fuse_candidates`, the signal
functions, `context_text`, `search_memory`, `_curate_memory_context_with_model`
prompt), `config.py` (`memory_vector_distance_ceiling`, `memory_rrf_k`),
`response_contracts.py` (memory response and recall contracts),
`tests/integration/test_north_star_memory_pass.py` (`_fake_memory_embedding`
upgraded to non-degenerate distinct vectors).

**Acceptance.**
- `build_memory_context` is deterministic for identical inputs and DB state
  (stable `ORDER BY ... id` on every query; RRF tie-break by ref).
- Lexical retrieval uses `tsvector`/`ts_rank_cd`; no Python term-overlap remains.
- `test_hybrid_retrieval_requires_multiple_signals`: a candidate that ranks
  first only under fused RRF is in the pool; the same suite, run with any single
  signal disabled, fails to surface the correct answer — i.e. vector-only and
  keyword-only both fail.
- Search and recall return every kind: semantic assertion, episode, reasoning
  trace, action trace, procedure, project state, negative memory, hot index,
  topic block, conflict.
- Curation accounts for every candidate; a test asserts
  `selected + omitted == candidate pool`.

---

### WS-5 — Reasoning-Trace And Negative Memory

**Problem.** `memory_reasoning_traces` exists but is never written or retrieved.
There is no `negative` assertion type. The North Star requires both.

**Target.** Reasoning traces are written, retrieved, and consolidated. Negative
knowledge is a first-class assertion family.

**Design.**

*Reasoning traces.* `record_reasoning_trace(db, *, scope_key, trace_type,
task_summary, trace_summary, outcome, evidence_id, ...)` writes a
`MemoryReasoningTraceRecord`. It is called from the chat-turn path (a
`diagnostic` or `successful_pattern` trace per turn that ran callables) and from
the extraction worker (the model may emit `reasoning_trace` candidates).
`build_memory_context` retrieves them (WS-4 `reasoning_traces` kind).
Consolidation (WS-8) promotes repeated `successful_pattern` traces to procedure
candidates and `failure`/`user_correction` traces to negative-memory candidates.

*Negative memory.* `assertion_type` gains `negative` (WS-1 registry declares the
`negative.*` predicate family: `rejected_approach`, `invalid_assumption`,
`already_checked`, `unsafe_operation`, `known_bad_path`). Negative assertions
flow through the standard candidate -> review -> active lifecycle. They are
`coexist` (a scope accumulates many). Recall surfaces them as the
`negative_memory` kind; `context_text` renders them under a "do not repeat"
heading; the hot index "do not repeat" section (WS-9) pulls active negative
assertions for the scope.

**Files.** `memory.py` (`record_reasoning_trace`, retrieval of both kinds,
extraction prompt, consolidation), `persistence.py` (CHECK constraint widening
for `negative`), `action_runtime.py` and `app.py` (chat-turn reasoning-trace
emission), `response_contracts.py` (`SurfaceMemoryReasoningTraceContract`,
`negative_memory` section).

**Acceptance.**
- Tests: a turn that runs callables writes a reasoning trace; a `negative`
  candidate flows through review to active and is recalled as `negative_memory`;
  consolidation promotes repeated successful traces to procedure candidates and
  failure traces to negative-memory candidates.
- A recall test proves negative memory affecting current work appears in the hot
  index "do not repeat" section.

---

### WS-6 — Unified Memory Event Stream And Deletion Audit

**Problem.** `evt.memory.*` events are persisted only on the turn path;
HTTP/capability/worker mutations discard them. `EventRecord` requires a
`turn_id`, so it structurally cannot carry non-turn memory events.

**Target.** Every memory mutation, from every entry path, appends to one
queryable `memory_events` log. Deletion audit is complete.

**Design.** Add the `memory_events` table (see Data Model). Add one helper:

```python
def emit_memory_events(
    db: Session,
    *,
    events: Sequence[dict[str, Any]],
    entry_path: str,                 # "turn" | "http" | "capability" | "worker"
                                     # | "proactive" | "consolidation"
    actor_id: str,
    scope_key: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
    source_turn_id: str | None = None,
) -> None:
    """Persist memory mutation events to the memory_events log."""
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            raise MemoryEventError(f"malformed memory event: {event!r}")
        db.add(MemoryEventRecord(
            id=new_id_fn("mze"), event_type=event_type, scope_key=scope_key,
            actor_id=actor_id, entry_path=entry_path,
            subject_refs=_event_subject_refs(payload), payload=payload,
            source_turn_id=source_turn_id, created_at=now))
```

Every memory mutation entry point calls `emit_memory_events` with its
`entry_path`: the chat-turn block, every `/v1/memory/*` HTTP mutation handler,
`_execute_memory_capability`, the extraction and maintenance workers,
`_apply_remember_decision`, and `consolidate_memory`. The turn-path
`EventRecord` loop for `evt.memory.*` is removed — those events go to
`memory_events` only. A malformed event is a defect (`MemoryEventError`), not a
silent drop.

New endpoint `GET /v1/memory/events` (filter by `scope_key`, `event_type`,
time range) and capability `cap.memory.events`.

Deletion audit: `_record_version` and `_record_deletion` already cover
retract/delete/privacy-delete/redact; WS-3 adds scope-binding versioning; the
`ck_memory_version_canonical_table` widening lets conflict-set and salience
mutations record versions too. The privacy-delete content scrubbing and
projection invalidation are already correct and are kept.

**Files.** `persistence.py` (`MemoryEventRecord`), `memory.py`
(`emit_memory_events`, `_event_subject_refs`), `app.py` (all memory HTTP
handlers, chat-turn block, `GET /v1/memory/events`), `action_runtime.py`
(`_execute_memory_capability`), `worker.py`, `proactivity.py`,
`capability_registry.py` (`cap.memory.events`), `response_contracts.py`.

**Acceptance.**
- Tests: a memory mutation via HTTP, via a capability, via the worker, and via
  proactive deliberation each write `memory_events` rows; a turn-driven mutation
  writes the same event types to `memory_events`.
- No memory mutation function's returned event list is discarded; grep confirms
  every call site passes the list to `emit_memory_events`.
- `GET /v1/memory/events` returns the stream filtered by scope.

---

### WS-7 — Action-Trace Completion

**Problem.** `MemoryActionTraceRecord` is created only in the chat-turn path.
Proactive action execution writes none. Denied/expired actions that produced no
user-turn evidence get no trace.

**Target.** Every action — proposal, policy decision, approval, execution,
outcome, undo — produces or updates an action trace, from every execution path.

**Design.** Extract the chat-turn trace-creation block at `app.py:7350` into
`memory.py` `record_action_trace(db, *, action_attempt, scope_key,
primary_evidence_id, source_turn_id, trace_type, now, new_id_fn)`. Call it from:
the chat-turn path (as today); `process_proactive_action_execution_due` in
`proactivity.py` (currently writes no memory state); and the
denied/expired-action paths in `action_runtime.py`. `_update_memory_action_traces`
(already wired at nine call sites) continues to update outcomes after async
execution. Proactive traces use `scope_key=f"proactive:{case.id}"` and a
proactive-observation evidence row as `primary_evidence_id`.

**Files.** `memory.py` (`record_action_trace`), `app.py` (chat-turn block calls
the shared function), `proactivity.py` (`process_proactive_action_execution_due`),
`action_runtime.py` (denied/expired paths).

**Acceptance.**
- Tests: successful, failed, denied, and undone action traces; proactive action
  execution creates trace evidence; async worker execution updates the existing
  trace outcome.
- Trace recall excludes the current session unless explicitly requested
  (existing behavior preserved).

---

### WS-8 — Projection And Consolidation Job System

**Problem.** Only `embedding` jobs are enqueued. `context_block`, `hot_index`,
`topic_block`, `project_state`, `export` jobs are never enqueued, so
`process_memory_maintenance_job` is dead. Graph and symbol projections are not
written. Consolidation has no autonomous trigger.

**Target.** Every projection kind in the CHECK constraint is enqueued and
consumed. Consolidation runs on real triggers. Projection health is honest.

**Design.**

*Synchronous projections.* `_record_projection_rows` (run inside the activation
transaction) writes keyword (`search_document` for the `tsvector` generated
column), entity, temporal, and symbol projections. Symbol projection extraction:
when an assertion's scope is a repo, tokenize identifier-like and path-like
substrings of its text into `memory_symbol_projections`.

*Async projections.* `embedding` stays a job (external API call). `graph`
becomes a job enqueued when a relationship is created or invalidated; the
handler computes BFS to depth 3 over `memory_relationships` and upserts
`memory_graph_projections` rows `(source_entity_id, reachable_entity_id,
distance, path)`.

*Consolidation triggers.* `enqueue_consolidation_job(db, *, scope_key, kind,
now)` inserts a `MemoryProjectionJobRecord`. It is called by autonomous
triggers, each gated by `resolve_memory_policy(operation="consolidate")`:
- session rotation (`app.py` rotation path),
- candidate backlog crossing `ARIEL_memory_consolidation_candidate_threshold`
  (checked when a candidate is proposed),
- conflict backlog,
- a scheduled worker cadence (`ARIEL_memory_consolidation_interval_seconds`,
  enqueued by a worker tick that finds the scope's last consolidation stale).

The `projection_kind` CHECK is reconciled (see Data Model);
`process_unsupported_memory_projection_job` remains as the dead-letter for any
out-of-band kind.

`projection_health` in recall reports `failed_projection_jobs`,
`dead_letter_projection_jobs`, and `stale_projection_count` (assertions whose
`source_memory_version` lags their canonical version).

**Files.** `memory.py` (`_record_projection_rows`, symbol/tsvector writers,
`enqueue_consolidation_job`, `projection_health`), `worker.py` (graph job
handler, scheduled consolidation tick), `app.py` (rotation trigger),
`persistence.py` (CHECK reconciliation, `memory_keyword_projections` columns),
`config.py` (consolidation thresholds and cadence).

**Acceptance.**
- Tests: a relationship change enqueues and completes a `graph` job; recall uses
  multi-hop graph results; symbol projection rows are written for repo-scoped
  assertions and used in retrieval; session rotation, candidate backlog, and the
  scheduled cadence each enqueue a consolidation job; `consolidate_memory` runs
  from the worker and rebuilds hot index and topic blocks.
- No `projection_kind` exists in a CHECK without an enqueue and a consume path.
- `projection_health` reflects a seeded dead-lettered job.

---

### WS-9 — Hot Index And Topic Budget Enforcement

**Problem.** Hot index content is unbounded JSON; `memory.md` mandates a
1,500-token default and 2,500-token hard cap.

**Target.** Hot index is rebuilt within an enforced token budget; every entry
carries source ids; topic context blocks are well-formed.

**Design.** `consolidate_memory` hot-index rebuild measures content with the
shared tokenizer used for `max_context_tokens` and evicts lowest-salience
entries until the content fits `ARIEL_memory_hot_index_budget_tokens` (default
1,500). It is a defect (`MemoryProjectionError`) if a rebuilt block exceeds
`ARIEL_memory_hot_index_hard_max_tokens` (default 2,500). Every hot-index entry
carries `source_assertion_ids` or a `topic_id` pointer — never large verbatim
content. A `topic`-type `MemoryContextBlockRecord` requires a non-null
`topic_id`. The hot index includes the WS-5 "do not repeat" negative-memory
section.

**Files.** `memory.py` (`consolidate_memory` hot-index/topic rebuild),
`config.py` (budget settings), `persistence.py` (topic-block `topic_id` CHECK).

**Acceptance.**
- Tests: a rebuilt hot index stays within the default budget; exceeding the hard
  max raises `MemoryProjectionError`; every entry carries ids/pointers; a
  `topic` block without `topic_id` is rejected.

---

### WS-10 — Long-Memory Eval Suite And Regression Gate

**Problem.** `run_memory_eval` is a real harness but ships no fixtures; no test
fails under degenerate retrieval; eval metrics are incomplete.

**Target.** A committed long-memory eval suite, run by `make verify`, with
adversarial cases and the full metric set from `memory.md`.

**Design.** Add `tests/integration/test_memory_eval_suite.py` and a fixture
module `tests/fixtures/memory_eval_cases.py` (canonical eval cases as data). The
suite seeds canonical memory, runs `run_memory_eval`, and asserts pass. Required
cases (from `memory.md`, all implemented):

- vector similarity alone selects the wrong memory; RRF + lexical/temporal
  corrects it,
- keyword match alone selects the wrong memory,
- temporal validity changes the answer,
- a conflict must be surfaced as uncertainty,
- the correct answer is to abstain,
- a correction/supersession case,
- a deletion/privacy-deletion compliance case,
- a `no_memory` mode case,
- a proactive-feedback case,
- a negative-memory adherence case,
- a graph-relationship reasoning case,
- a hot-index budget-pressure case.

`run_memory_eval` is extended to record the full `memory.md` metric set:
`answer_accuracy`, `candidate_recall`, `curation_precision`, selected/omitted
counts, `conflict_handling_accuracy`, `context_tokens`, and the
extraction/retrieval/curation/projection/consolidation latencies.

`make verify` runs the suite. The suite fails if vector-only or keyword-only
retrieval would pass — enforced by running the adversarial cases against
single-signal-disabled retrieval and asserting failure.

**Files.** `tests/integration/test_memory_eval_suite.py`,
`tests/fixtures/memory_eval_cases.py`, `memory.py` (`run_memory_eval` metrics),
`Makefile` (`verify` target includes the suite).

**Acceptance.**
- The suite is committed, runs in `make verify`, and passes.
- The suite fails under vector-only or keyword-only retrieval.
- `MemoryEvalRunRecord` carries the full metric set.

## File And Module Map

| File | Change |
|------|--------|
| `src/ariel/memory.py` | Predicate registry; `resolve_memory_policy` (replaces 2 functions); conflict closure; RRF retrieval rewrite; reasoning-trace and negative-memory support; `emit_memory_events`; `record_action_trace`; consolidation triggers; projection writers; eval metrics. |
| `src/ariel/persistence.py` | `MemoryEventRecord`; CHECK-constraint edits; `memory_keyword_projections` `tsvector`; `conflict_type` column. |
| `src/ariel/app.py` | Chat-turn block uses `resolve_memory_policy` and shared trace/event helpers; all `/v1/memory/*` mutation handlers persist events; `GET /v1/memory/events`; candidate request drops `is_multi_valued`. |
| `src/ariel/worker.py` | Graph projection job; scheduled consolidation tick; projection-kind reconciliation. |
| `src/ariel/proactivity.py` | Pass `proactive_case_id` into policy/propose; emit action traces; emit memory events. |
| `src/ariel/action_runtime.py` | `_execute_memory_capability` persists events; denied/expired action traces; reasoning-trace emission. |
| `src/ariel/capability_registry.py` | `cap.memory.propose` drops `is_multi_valued`; `cap.memory.set_scope_mode` adds `expires_at`; `cap.memory.events`. |
| `src/ariel/response_contracts.py` | `memory.sota.v2` shapes; reasoning-trace, negative-memory, event contracts; recall feature vector. |
| `src/ariel/config.py` | Vector ceiling, RRF `k`, consolidation thresholds/cadence, hot-index budgets. |
| `alembic/versions/20260515_0030_memory_completion_cutover.py` | The single cutover migration. |
| `tests/integration/test_north_star_memory_pass.py` | Rewrites for conflict closure, hybrid retrieval, mode precedence; non-degenerate embedding fixture. |
| `tests/integration/test_worker_memory_jobs.py` | Graph job, consolidation triggers, projection-kind reconciliation. |
| `tests/integration/test_memory_eval_suite.py` | New long-memory eval suite. |
| `tests/fixtures/memory_eval_cases.py` | New eval case fixtures. |
| `docs/modules/memory.md` | Apply the Amendments section. |
| `docs/modules/memory-sota-completion-plan.md` | Deleted. |

`memory.py` stays a flat module. If it crosses a concrete skim-ability limit
during this work, split along the North Star's named axes
(`memory_retrieval.py`, `memory_consolidation.py`, `memory_eval.py`) — not
before.

## The Cutover Migration

`alembic/versions/20260515_0030_memory_completion_cutover.py`, standard repo
style (`from __future__ import annotations`; `revision = "0030"`;
`down_revision = "0029"`; explicit `upgrade()`/`downgrade()`). It:

- creates `memory_events` with its check constraints and indexes,
- adds `memory_conflict_sets.conflict_type` (`server_default='value_contradiction'`,
  then drop the default) with `ck_memory_conflict_set_type`,
- drops and recreates `ck_memory_assertion_type` to add `'negative'`,
- drops and recreates `ck_memory_scope_binding_scope_type` without `'session'`,
- drops and recreates `ck_memory_version_canonical_table` with the four added
  tables,
- drops and recreates `ck_memory_projection_job_kind` with the reconciled set,
- adds `memory_keyword_projections.search_document` and the `search_vector`
  generated column, creates the GIN index, drops `weighted_terms`, widens
  `canonical_table`,
- ensures the pgvector HNSW index on `memory_embedding_projections.embedding`.

`downgrade()` reverses each step. Because no production memory rows exist, no
data migration is needed; the migration is pure DDL.

## Master Acceptance Criteria

The cutover is complete only when all of the following hold.

**Correctness.**
- Cardinality is never read from input; only from `resolve_predicate_spec`.
- The active store can never hold two active single-valued assertions for one
  subject/predicate/scope (registry + partial unique index + tests).
- Every `conflicted` assertion belongs to exactly one `open` conflict set.
- Every conflict set reaches `resolved` or `ignored` once contradiction ends.
- `resolve_conflict` preserves winner version history and supersedes (not
  rejects) a previously-active loser.

**Policy and privacy.**
- One `resolve_memory_policy`; the two old functions are deleted.
- The strictest mode in the scope chain wins; expiry works; binding changes are
  audited.
- `no_memory`/`temporary` block recall, extraction, proactive writes,
  action-trace writes, and consolidation across every scope type.
- Privacy-delete scrubs content and blocks projection rebuilds (preserved).

**Retrieval.**
- Candidate retrieval is deterministic and hybrid via RRF.
- Lexical retrieval is Postgres full-text; no Python term-overlap remains.
- Graph and symbol projections are written and read; no dead projection code.
- Search and recall return every memory kind.
- The eval suite fails under vector-only or keyword-only retrieval.

**Completeness.**
- Every `projection_kind` is enqueued and consumed.
- Consolidation runs on autonomous triggers.
- Action traces are written from every execution path.
- The hot index is budget-enforced.
- Every memory mutation writes `memory_events` from every entry path.
- The long-memory eval suite runs in `make verify` and passes.

**Verification.**
- `make verify` passes (ruff, ruff format, mypy strict, pytest).
- A grep confirms `session_allows_memory_operation`,
  `scope_allows_memory_operation`, the naive keyword block, and `is_multi_valued`
  input fields are gone.
- `docs/modules/memory-sota-completion-plan.md` is deleted.

## Key Decisions

- **Predicate type behavior is deterministic, not AI-judged.** The model authors
  predicate strings and values; the registry resolves cardinality, conflict
  policy, value kind, sensitivity, and decay. This is the root-cause fix for the
  conflict defect and the [`../ai-first.md`](../ai-first.md)-correct split.
- **Unknown predicates fail safe.** They resolve single-valued with `conflict`
  policy — a false conflict (a question to the user) is safer than a missed
  contradiction (a corrupt store).
- **Strictest mode wins.** Effective memory mode is the most restrictive mode in
  the scope chain; specificity only chooses which scope is reported as
  controlling. A broad `no_memory` is never overridden by a narrow `normal`,
  because `no_memory` is a hard privacy intent.
- **Session mode lives on `SessionRecord`, not in the bindings table.** One
  source of truth per scope; `memory_scope_bindings` covers the other five scope
  types.
- **RRF builds the candidate pool; AI curation owns relevance.** Reciprocal Rank
  Fusion is a deterministic transport-ordering mechanism, not a relevance brain.
  No learned reranker is added.
- **Confidence decay is a feature, not a filter.** It feeds recall features and
  the consolidation staleness trigger; it never silently drops a memory.
- **Memory events are a non-turn-scoped log.** `memory_events` is the single
  memory event stream; `EventRecord` keeps the conversational stream.
- **Negative memory is a typed assertion family**, not a separate table — it
  reuses the full lifecycle, review, conflict, and projection machinery.
- **Hard cutover.** No legacy functions, no fallback retrieval, no compatibility
  shapes, no dual paths. There is no production memory data to preserve.

## Sequencing

Reviewable commits; no intermediate state is production-shippable.

1. **WS-1** — predicate registry. Foundational; everything else depends on it.
2. **Migration `0030`** — land the schema with WS-1.
3. **WS-2, WS-3** — correctness and privacy. Parallel after WS-1; both touch
   `propose_memory_candidate` and mutation functions, so coordinate.
4. **WS-6, WS-7** — event stream and action traces. Mechanical; can land beside
   WS-2/WS-3.
5. **WS-8** — projection and consolidation jobs. Needed before WS-4 graph/symbol
   signals are real.
6. **WS-4, WS-5** — hybrid retrieval and new memory kinds. The largest
   workstream; depends on WS-1, WS-3, WS-8.
7. **WS-9** — hot-index budgets. Depends on WS-8 consolidation.
8. **WS-10** — eval suite and `make verify` gate. Last; proves the whole.

## Risks

- **`build_memory_context` rewrite is large.** Mitigation: WS-4 lands behind the
  committed eval suite (WS-10 fixtures land first as `xfail`, flipped on as WS-4
  completes); the candidate pool is unit-tested independently of curation.
- **`tsvector` language configuration.** `'english'` is assumed; if memory
  content is multilingual this needs revisiting. Acceptable for single-user v1.
- **Graph BFS cost.** Depth-3 BFS on a large entity graph can be expensive; the
  graph projection job bounds it and runs async. Revisit depth if projection
  latency regresses in the eval metrics.
- **Predicate registry coverage.** A registry that is too sparse pushes many
  predicates to the default spec. Mitigation: the extraction prompt is given the
  registry vocabulary; consolidation reports unregistered predicates that recur,
  so the registry grows from evidence.

## Amendments To memory.md

Apply these edits to [`memory.md`](memory.md) so the North Star and this cutover
stay MECE:

- **Data Model / Required Invariants** — replace "Multi-valued predicates must
  declare that they are multi-valued in the schema or type registry" with a
  pointer to the predicate registry as the authoritative declaration of
  cardinality, conflict policy, value kind, sensitivity default, and decay.
- **Retrieval** — state that hybrid candidate retrieval is Reciprocal Rank
  Fusion over the listed signals, deterministic with stable tie-breaking, and
  that each candidate carries a feature vector.
- **Memory Structure / Memory Types** — state that negative memory is an
  `assertion_type` value (`negative`) with a dedicated predicate family.
- **APIs And Commands / Events** — state that `evt.memory.*` events are
  persisted to the `memory_events` log, not the turn `EventRecord` stream, and
  add `GET /v1/memory/events`.
- **Data Model / Required Lifecycles** — note that conflict sets always reach a
  terminal state and that `conflict_type` is recorded.

## State Of The Art Positioning

A web survey of the agent-memory field — commercial memory layers, foundation-
model assistant memory, coding-agent memory, the academic literature, the
benchmark landscape, the research frontier, and user sentiment — was conducted
on 2026-05-16. This section records what it found and where this cutover's
implementation sits, so the document stays the single source of truth for the
subsystem's direction.

**The 2026 consensus architecture.** Serious memory systems have converged on
eight properties: (1) memory is a write -> manage -> read *lifecycle*, not a
store — the write side (extract, update, invalidate, consolidate, forget) is
what separates memory from RAG; (2) the working/episodic/semantic/procedural
taxonomy, increasingly plus negative memory; (3) lossless episodes kept beside
derived semantic facts, every fact carrying provenance; (4) hybrid multi-signal
retrieval — vector, lexical, entity/graph, recency, salience — never pure
vector; (5) explicit temporal modelling, ideally bi-temporal, with stale facts
invalidated rather than deleted; (6) consolidation off the hot path
("sleep-time" / background reflection); (7) a compact always-loaded index plus
lazily-loaded topic detail; (8) for coding work, a split between
version-controlled rules and machine-local memory, plus code-map memory.

**Where this implementation stands.** The completed cutover realises all eight:
the predicate registry (a typed schema more rigorous than any shipped product's
taxonomy), the episodic/semantic/procedural/negative/reasoning kinds, the
evidence -> assertion provenance chain, RRF hybrid retrieval over seven signals,
`valid_from`/`valid_to` temporal validity, autonomous consolidation jobs, the
hot-index plus topic-block projection, and the eval gate. On the two dimensions
the field is *weakest* at — a conflict lifecycle that always closes, and honest
evaluation in the build — this implementation is ahead of the shipped products.

**The honest gaps** (each is addressed by a Forward Opportunity below).
(a) Temporal modelling is three-quarters of a bi-temporal model: it tracks
valid-time and record-time but has no explicit transaction-time invalidation
usable for "as-of" queries. (b) `decay_half_life_days` is a declared retrieval
feature, not an active forgetting policy — and selective forgetting is the
capability the entire field fails. (c) Consolidation is mechanical (merge,
supersede, rebuild); it does not yet do reflective synthesis. (d) Repo-scoped
procedural memory lives only in the database; the coding-tool consensus is to
promote durable knowledge into version-controlled rule files.

**Benchmark discipline.** The field's public leaderboards are not trustworthy:
on the most-cited benchmark (LoCoMo) a plain full-context baseline outscores
every memory product, ground-truth defects are documented, and vendor
self-reports for the same system on the same benchmark span roughly 58-84%. The
credible external benchmarks to track are LongMemEval, BEAM, ConvoMem, and
MemoryAgentBench. The discipline they enforce — fixed backbone model, multiple
runs with variance, every category counted, and a full-context baseline always
included — is the standard the eval suite holds to (WS-10, extended by FO-1).

**The frontier, and why this design stays external.** The research frontier —
neural test-time memory (the Titans / Nested-Learning line), parametric memory
layers, test-time training, self-editing models — is moving memory *into model
weights*. None ships at production scale, and weight-resident memory is neither
auditable nor cleanly deletable. This cutover's external, evidence-backed,
inspectable store is the deliberately correct choice for a system that must
honour deletion, redaction, and scoped no-memory. Parametric memory is out of
scope until it is both auditable and production-proven (see Out Of Scope).

## Forward Opportunities And v2 Roadmap

These opportunities are **not part of the completed cutover** and are **not a
hard cutover**. Each is an independent, incremental enhancement on the finished
v1, shippable on its own behind the existing review, policy, and eval gates.
They are ordered by leverage.

### FO-1 — Adversarial Long-Memory Eval Expansion

**Motivation.** The field's failure modes are well documented: contradiction
resolution and selective forgetting fail almost universally; temporal and
multi-session reasoning are weak. The credible benchmarks (BEAM, ConvoMem,
MemoryAgentBench) earn their value by constructing *adversarial* cases that the
broken benchmarks cannot. WS-10 shipped a 12-case suite and the `run_memory_eval`
harness; it should grow into a genuine regression gate against every
field-identified failure mode.

**Target.** Extend `tests/fixtures/memory_eval_cases.py` and
`tests/integration/test_memory_eval_suite.py` with one adversarial case per
failure mode: a **knowledge-update chain** (a single-valued fact revised three
times — recall must return only the latest, never a superseded value); a
**multi-message-evidence** case (the answer requires fusing three to six
messages); a **contradiction** case (an open conflict must surface as
uncertainty, never as settled fact); a **deletion-durability** case (a
privacy-deleted fact must not resurface through any projection or the hot
index); a **temporal-decisive** case (validity windows change the answer); and a
**discrimination guard** (a case a recency-only or full-context strategy gets
wrong). Extend `run_memory_eval` to record per-failure-mode pass rates on
`MemoryEvalRunRecord`.

**Acceptance.** At least one case per field-identified failure mode; each
adversarial case fails under a degraded baseline and passes under hybrid
retrieval; per-mode metrics recorded; the suite stays in `make verify`.

### FO-2 — True Bi-Temporal Validity

**Motivation.** The central result of the temporal-knowledge-graph literature is
bi-temporal modelling: separating *valid-time* (when a fact holds in the world)
from *transaction-time* (when the system recorded it and later un-recorded it),
enabling both "what is true now" and "what did we believe at time T" queries,
with stale facts *invalidated* rather than deleted. `MemoryAssertionRecord`
already carries `valid_from`/`valid_to` (valid-time) and `created_at`
(record-time); the missing quarter is an explicit transaction-time invalidation
timestamp and the as-of query path.

**Target.** Add `invalidated_at` to `MemoryAssertionRecord`, set whenever an
assertion leaves `active` (superseded, retracted, deleted, conflicted loser) —
distinct from `valid_to` (a real-world end) and from the `memory_deletions`
audit row. Give `build_memory_context` an optional `as_of` parameter: with it,
recall reconstructs the belief state at that instant (assertions with
`created_at <= as_of` and `invalidated_at` null or `> as_of`). The temporal RRF
signal and `projection_health` consume it.

**Acceptance.** An assertion superseded at T carries `invalidated_at = T` with
its `valid_*` window intact; an `as_of` recall returns exactly the assertions
believed-active at that time; every non-active lifecycle transition sets
`invalidated_at`.

### FO-3 — Selective Forgetting And Unlearning Policy

**Motivation.** Across every credible benchmark, *selective forgetting* is the
worst-performing capability. Solving it would put this system ahead of every
shipped product. The implementation has reactive deletion (privacy-delete) and a
declared per-predicate `decay_half_life_days`, but no *active* policy that
proactively demotes stale, low-salience, never-reconfirmed knowledge. The
predicate registry already declares decay; the `memory_retention_policies` table
already exists.

**Target.** A forgetting pass inside `consolidate_memory`. For each active
assertion, compute the existing `effective_confidence` (confidence decayed by
age per the predicate's half-life) and combine it with salience and
last-verified age. Assertions below a configured floor are **demoted** — dropped
from the hot index and topic blocks, excluded from default recall — without
being destroyed: the evidence and episode remain, the assertion moves to `stale`
through the review lifecycle, and recall diagnostics record the reason. This is
"forget from recall," strictly separate from privacy-delete's "forget from
existence." Honour `memory_retention_policies` (`delete_after` / `review_after`).
Re-confirmation re-activates a demoted assertion.

**Acceptance.** A low-salience, long-unverified assertion is demoted out of
recall by consolidation with no operator action and an audited rationale;
demotion is reversible; privacy-deleted content provably cannot resurface; an
FO-1 forgetting case covers it.

### FO-4 — Reflective Consolidation

**Motivation.** Sleep-time compute and reflection (Generative Agents; agentic
memory-evolution work) show the highest-value memory work is *offline synthesis*
— deriving higher-order knowledge between interactions, not merely merging
duplicates. `consolidate_memory` today is mechanical: dedupe, supersede, rebuild
projections, and promote repeated successful traces to procedure candidates. It
does not synthesise insight.

**Target.** Add a reflection phase to `consolidate_memory`. Given a scope's
recent episodes, reasoning traces, and action traces, an AI judgment proposes
higher-order memory — synthesised insights (cross-assertion derived facts),
negative memory from repeated failure traces, and procedure candidates from
repeated successful patterns — all routed through the candidate -> review
lifecycle, never written active directly. Record it as an AI-judgment row with
input/selected/omitted sources, model, and prompt version. The phase respects
memory mode (no reflection under `temporary`/`no_memory`).

**Acceptance.** A scope with a pattern recurring across episodes yields a
reviewed insight, negative-memory, or procedure candidate that no single episode
stated; the reflection is fully auditable; it is policy-gated.

### FO-5 — Memory-To-Rules Promotion For Coding Continuity

**Motivation.** The cross-tool consensus for *coding* memory is a two-layer split
— machine-local auto-memory versus version-controlled, team-shared **rules**
(`AGENTS.md`, `.cursor/rules`, `CLAUDE.md`) — and the standard practice is to
*promote* durable knowledge from memory into a rule file once it must be
reliable and shareable. This system's repo-scoped procedural memory and the
`repo-conventions` topic block are the canonical analog, but they live only in
the database; they cannot be reviewed in a pull request or shared through the
repository.

**Target.** A new export-artifact projection that materialises a scope's active,
reviewed, repo-scoped procedural memory and `repo-conventions` topic content
into a versionable `AGENTS.md`-style file. The `MemoryExportArtifactRecord` /
`export_memory` machinery already exists; this is a new artifact kind. The file
is a **projection** — canonical state stays in the database per the North Star —
regenerated by consolidation. Optionally, ingest an existing `AGENTS.md` /
`CLAUDE.md` as trust-labelled, review-gated evidence, closing the loop.

**Acceptance.** Reviewed repo procedural memory round-trips to a versionable
rules file; the file is a rebuildable projection, never canonical; editing the
file does not mutate memory — only the memory APIs do.

### Sequencing

FO-1 first: it is cheap, it is the verification discipline, and it is how every
other opportunity is validated. Then FO-2 and FO-3 together — FO-2's
transaction-time makes FO-3's staleness reasoning exact. Then FO-4, then FO-5.
Each ships independently; none is a cutover.

### Out Of Scope

- **Parametric / weight-resident memory** (neural test-time memory, parametric
  memory layers, self-editing models) — not auditable, not cleanly deletable,
  not production-proven. Revisit only when both auditability and production
  evidence exist.
- **A dedicated graph database** — the `memory_relationships` tables and the
  graph projection are sufficient; the field shows no consensus that a graph
  database is required, and the North Star keeps PostgreSQL canonical.
- **External managed memory products** — reference models only, per the North
  Star's non-goals.
