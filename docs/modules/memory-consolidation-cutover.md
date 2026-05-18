# Memory Consolidation Cutover

## Scope

Phase 2 of the schema consolidation ([../schema-consolidation-cutover.md](../schema-consolidation-cutover.md)).
The memory subsystem holds 31 `memory_*` tables plus `project_state_snapshots`.
This cutover consolidates it to 25 `memory_*` tables: it removes one dead table
and one fully-derivable table, collapses three near-identical trace tables into
one, folds a 1:1 satellite into its parent, and moves embeddings to a `pgvector`
column.

A table-by-table deep-dive established that the rest of the memory schema earns
its place. The six `*_projection` tables are all live — written and read. The
topic, export, eval, and snapshot tables each have a distinct owner and active
readers. This cutover changes only the six tables a deep-dive found unjustified.

The memory subsystem shipped via [memory-completion-cutover.md](memory-completion-cutover.md)
(merged 2026-05-16). This cutover removes tables that doc and [memory.md](memory.md)
mandate; the required spec amendments are in the final section.

## Cutover Policy

Inherits [../schema-consolidation-cutover.md](../schema-consolidation-cutover.md).
Each move is one PR: one Alembic migration, the `persistence.py` model change,
the code change. No compatibility shim, no dual-write, no feature flag. Ariel
holds no production data; migrations drop and recreate tables freely with no
data-migration step. Every migration has a working `downgrade()`. Each PR runs
`ruff`, `mypy`, and the full `pytest` suite to green, and tests the migration up
and down.

## Moves

Five moves. The migrations form a linear Alembic chain in the order below — dead
and redundant tables first, structural merges last. Moves 1, 3, and 4 each
rewrite `ck_memory_version_canonical_table`; every migration recreates that
constraint against the table set current at its point in the chain.

| # | Move | Tables |
|---|---|---|
| 1 | Drop `memory_deletions` | −1 |
| 2 | Drop `memory_reviews` | −1 |
| 3 | Fold `memory_salience` into `memory_assertions` | −1 |
| 4 | Collapse `memory_episodes` + `memory_reasoning_traces` + `memory_action_traces` | −2 |
| 5 | Move embeddings to a `pgvector` column on `memory_assertions` | −1 |

Result: 31 → 25 `memory_*` tables. `memory_projection_jobs` → `background_tasks`
is deferred to Phase 4 (job-queue unification).

### Move 1 — Drop `memory_deletions`

`memory_deletions` is a strict, fully-derivable subset of `memory_versions`.
Every `_record_deletion` call (`memory.py`, 7 sites) is paired with a
`_record_version` call on the same record, and `MemoryVersionRecord` carries the
same fields: `canonical_table`, `canonical_id`, `change_type`, `actor_id`,
`reason`, `redaction_posture`, `projection_invalidation`, `created_at`.
`memory_versions.change_type` already enumerates `retracted`/`deleted`/
`privacy_deleted`/`redacted`. `memory_deletions` stores no fact not in
`memory_versions`. Its one reader is `list_memory` (`memory.py:6483`).

Schema: drop `memory_deletions` and its index; remove `'memory_deletions'` from
`ck_memory_version_canonical_table` (`persistence.py:1686`); delete
`MemoryDeletionRecord`.

Code:
- Delete `_record_deletion` (`memory.py:878-903`) and its 7 call sites. Each
  call's `projection_invalidation` default is already duplicated into the paired
  `_record_version` call — nothing is lost.
- Replace the `list_memory` deletions read (`memory.py:6482-6486`) with a query
  over `MemoryVersionRecord` filtered to the four deletion `change_type` values,
  ordered by `created_at desc`, limit 50.
- In the `deletions` serializer (`memory.py:6645-6658`) map `canonical_table →
  target_table`, `canonical_id → target_id`, `change_type → deletion_type`. The
  `GET /v1/memory` response shape is unchanged.
- Drop the import (`memory.py:25`); remove `memory_deletions` from `db.py`
  `REQUIRED_TABLES` and its constraint registries.

Tests: `test_north_star_memory_pass.py:1136,1914,1926` count `MemoryDeletionRecord`
rows — rewrite as `MemoryVersionRecord` queries with the mapped `change_type`/
`redaction_posture` (value-identical substitution); update the import at `:41`.

### Move 2 — Drop `memory_reviews`

`memory_reviews` is write-only. `_record_review` (`memory.py:814-833`) is the
only writer (9 call sites); there is no `select()` of `MemoryReviewRecord`
anywhere in `src/` and no `memory/review*` or `memory/history` endpoint. Its
signals are preserved elsewhere: review *state* is `memory_assertions.lifecycle_state`
(`candidate`/`conflicted`); review *history* is `memory_versions`
(`change_type='reviewed'`) plus the `evt.memory.review_required` row in
`memory_events`. A review-history surface, if ever built, is reconstructed from
those — no separate table is needed.

Schema: drop `memory_reviews` and its indexes; delete `MemoryReviewRecord`. The
`downgrade()` recreates the table at its post-`0023` schema (migration `0023`
altered the `decision` CHECK).

Code: delete `_record_review` and all 9 call sites (`memory.py:2294,2829,2949,
3782,3907,3962,4252,4272,4367`); drop the import (`memory.py:40`); remove
`memory_reviews` from `db.py` `REQUIRED_TABLES` and both constraint registries.

Tests: six assertions across `test_north_star_memory_pass.py:477,2614,2945,3949`,
`test_worker_memory_jobs.py:1485`, `test_proactive_runtime_completion.py:482`
assert a review was recorded. **Rewrite, do not delete** — re-point each at the
assertion `lifecycle_state` or a `memory_events` row of type
`evt.memory.review_required`. This is the move's main labour.

### Move 3 — Fold `memory_salience` into `memory_assertions`

`memory_salience` is strictly 1:1 with `memory_assertions` (`assertion_id`
unique, FK `RESTRICT`). Every write and delete is slaved to the assertion
lifecycle; all four read sites join 1:1. It is a satellite with no independent
lifecycle — `cleanliness.md`: a 1:1 satellite should be columns.

Schema: add to `memory_assertions` —

```
salience_user_priority  String(32)  NOT NULL  default "none"
salience_score          Float       NOT NULL  default 0.0
salience_signals        JSONB       NOT NULL  default dict
```

Constraints `ck_memory_assertion_salience_user_priority`
(`IN ('none','pinned','deprioritized')`) and
`ck_memory_assertion_salience_score_non_negative` (`>= 0.0`). Delete
`MemorySalienceRecord`; remove `'memory_salience'` from
`ck_memory_version_canonical_table` (`persistence.py:1690`).

Code (all `memory.py`): `_record_salience` (~`1731-1767`) collapses to in-place
attribute writes on the loaded `MemoryAssertionRecord` — no second row, no
`select`. `_delete_projection_rows` (~`1208`) drops its salience handling.
`set_assertion_priority` (~`3505`), the forgetting pass (~`4304`), consolidation
(~`4991`), and recall fusion (~`8235`) read the attributes off the loaded
assertion. Drop imports (`memory.py:42` and the two affected tests); remove
`memory_salience` from `db.py` `REQUIRED_TABLES` and constraint registries.

Tests: `test_s5_pr01_acceptance.py:388`, `test_worker_memory_jobs.py:1150-1199`
construct `MemorySalienceRecord` directly — set the new columns on the assertion.

Open fork — `salience_score` is written as literal `0.0` everywhere in `src/`,
and `salience_signals` is a denormalized cache of `assertion_type`/`confidence`/
`user_priority` (already columns on the assertion). The move folds all three for
a mechanical cutover; a follow-up may drop `salience_score`/`salience_signals` as
speculative surface, pending whether a future ranking feature populates `score`.

### Move 4 — Collapse the three trace tables into `memory_episodes`

`memory_episodes`, `memory_reasoning_traces`, and `memory_action_traces` are the
same architectural concept — an append-mostly, evidence-anchored record of
something that happened, recalled but never re-derived. They share a seven-column
core, have **zero inbound SQL foreign keys**, and do not participate in the
version or projection-job machinery. Their per-class differences are a thin tail
that a discriminator plus a compound CHECK absorb.

Merged table — one `memory_episodes` with an `episode_class` discriminator
(`source` / `reasoning` / `action`):

```python
class MemoryEpisodeRecord(Base):
    __tablename__ = "memory_episodes"

    id:                    Mapped[str]            # String(32), PK
    episode_class:         Mapped[str]            # String(32) NOT NULL  -- discriminator
    episode_type:          Mapped[str]            # String(32) NOT NULL  -- was *_type per table
    scope_key:             Mapped[str]            # Text NOT NULL, indexed
    summary:               Mapped[str]            # Text NOT NULL
    title:                 Mapped[str | None]     # Text          -- source-only
    outcome:               Mapped[str | None]     # String(32)    -- enum; NULL for source
    primary_evidence_id:   Mapped[str]            # FK memory_evidence RESTRICT, indexed
    source_turn_id:        Mapped[str | None]     # FK turns RESTRICT, indexed
    action_attempt_id:     Mapped[str | None]     # FK action_attempts RESTRICT, indexed -- action-only
    capability_id:         Mapped[str | None]     # String(128)   -- action-only
    occurred_at:           Mapped[datetime | None]# tz            -- source-only
    valid_from:            Mapped[datetime | None]# tz
    valid_to:              Mapped[datetime | None]# tz
    lifecycle_state:       Mapped[str]            # String(32) NOT NULL default "active"
    related_entity_ids:    Mapped[list]           # JSONB default list
    related_assertion_ids: Mapped[list]           # JSONB default list
    result_refs:           Mapped[dict]           # JSONB default dict -- action; {} otherwise
    metadata_json:         Mapped[dict]           # JSONB default dict -- holds task_summary, turn_id
    created_at:            Mapped[datetime]       # tz NOT NULL, indexed
    updated_at:            Mapped[datetime]       # tz NOT NULL, indexed
```

Six named CheckConstraints:

1. `ck_memory_episode_class` — `episode_class IN ('source','reasoning','action')`.
2. `ck_memory_episode_type_for_class` — `episode_type` valid for its class
   (preserves the three disjoint `*_type` enums).
3. `ck_memory_episode_outcome_for_class` — `outcome` NULL for `source`;
   class-scoped enum for `reasoning` (`succeeded/failed/corrected/unknown`) and
   `action` (`succeeded/failed/denied/undone/unknown`).
4. `ck_memory_episode_lifecycle_state` — the union of the three originals
   (`active/stale/superseded/retracted/deleted/privacy_deleted`).
5. `ck_memory_episode_class_fields` — compound: `source` requires
   `title`+`occurred_at` and forbids action fields; `reasoning` requires
   `outcome` and forbids action fields and `occurred_at`; `action` requires
   `outcome` and forbids `occurred_at`/`title`.
6. `ck_memory_episode_valid_interval` — `valid_from < valid_to` when both set.

Indexes: carry over `scope_key`, `primary_evidence_id`, `source_turn_id`,
`action_attempt_id`, `created_at`, `updated_at`; add `episode_class` and a
composite `(episode_class, lifecycle_state)` — every recall query filters
`lifecycle_state='active'` and now also branches on `episode_class`.

`task_summary` (unique to reasoning traces) moves to
`metadata_json["task_summary"]` — a payload field, never filtered or joined.

Schema/migration: drop the three tables; create the merged `memory_episodes`.
Edit the `canonical_table` CHECK enums that list the two retired names — at this
point in the chain that is **7** tables (`memory_versions`,
`memory_sensitivity_labels`, `memory_keyword_projections`,
`memory_entity_projections`, `memory_temporal_projections`,
`memory_symbol_projections`, `memory_topic_members`); `memory_deletions` is
already gone from Move 1. Each becomes `…'memory_episodes'…` with the two trace
names removed.

Code:
- `persistence.py`: delete `MemoryReasoningTraceRecord` and
  `MemoryActionTraceRecord`; replace `MemoryEpisodeRecord`; edit the 7 CHECK
  literals.
- `memory.py` write helpers: `record_turn_episode` sets `episode_class="source"`;
  `record_action_trace` sets `episode_class="action"` and adds
  `episode_class=="action"` to its upsert-on-`action_attempt_id` lookup;
  `record_reasoning_trace` sets `episode_class="reasoning"` and writes
  `metadata_json={"task_summary": …}`. Helper signatures stay unchanged.
- `memory.py` recall: collapse the three `_TABLE_*` constants to one; merge the
  three-way branches in `_lexical_signal`, `_recency_signal`, fused-pool
  hydration, the recall rails, and candidate-payload build into one branch that
  switches on `episode_class`. Keep the per-class `to_tsvector` documents and
  per-class `kind`/payload shapes byte-identical so recall ranking does not
  drift.
- `action_runtime.py`: `_update_memory_action_traces`,
  `_memory_action_trace_payloads` — rename the class, add
  `episode_class=="action"` to the lookups.
- `proactivity.py`: the action-trace dedupe query — rename, add
  `episode_class=="action"`.
- `app.py`: imports only.
- **Preserve the external vocabulary**: recall `kind` stays `episode`/
  `reasoning_trace`/`action_trace`; memory-context bundle keys stay
  `episodes`/`reasoning_traces`/`action_traces`. App rendering, the
  memory-context schema, and tests asserting `kind` are untouched. ID prefixes
  stay `mep`/`mrt`/`mat`.

Tests: `test_north_star_memory_pass.py` (the `_seed_*` helpers construct the old
classes — set `episode_class`, move `task_summary` into `metadata_json`);
`test_email_decluttering_action_runtime.py`, `test_proactive_runtime_completion.py`
(rename `MemoryActionTraceRecord`); any schema-count test (−2). Tests asserting
recall `kind` values stay green because the vocabulary is preserved.

### Move 5 — Move embeddings to a `pgvector` column

`memory_embedding_projections` is effectively a 1:1 satellite of
`memory_assertions` — one live `projection_version` per assertion, one
provider/model. Storing the embedding on the assertion drops a table and a join
and uses `pgvector` natively.

Schema: add to `memory_assertions` — `embedding Vector(MEMORY_EMBEDDING_DIMENSIONS)`
nullable, `embedding_model String(128)` nullable, `embedding_source_version
Integer` nullable; create an HNSW index `USING hnsw (embedding vector_cosine_ops)`.
Drop `memory_embedding_projections`; delete `MemoryEmbeddingProjectionRecord`.
The column is nullable because the embedding job populates it asynchronously
after activation — a NULL embedding is the natural "embedding pending" signal.

Code (`memory.py`): `_record_projection_rows` still enqueues the `embedding`
projection job. `process_memory_projection_job` (~`1516-1525`) writes the vector
onto the `MemoryAssertionRecord` row instead of inserting a projection row.
`_vector_signal` (~`7489-7529`) becomes a join-free `ORDER BY embedding <=> :q`
filtered to `embedding.is_not(None)`. `_delete_projection_rows` (~`1026`) drops
the embedding-id block. `projection_health` (~`7502`) reads embedding staleness
off the assertion column.

Tests: `test_north_star_memory_pass.py`, `test_worker_memory_jobs.py`,
`test_memory_eval_suite.py`, `test_s5_pr01_acceptance.py`,
`test_s8_pr01/02_acceptance.py` — update assertions touching
`MemoryEmbeddingProjectionRecord`; vector-retrieval behaviour tests pass
unchanged if `_vector_signal` keeps its contract.

Risk: between activation and embedding-job completion
`memory_assertions.embedding IS NULL` — `_vector_signal` must filter
`is_not(None)`. Verify the installed `pgvector` builds an HNSW index over a
nullable column on PostgreSQL 16.

## Sequencing and coupling

The five migrations chain linearly in the numbered order. `ck_memory_version_canonical_table`
is rewritten by Moves 1, 3, and 4; each migration drops and recreates it against
the table set current at that point — use module-level before/after constant
strings (migration `0030`'s pattern) to keep `upgrade`/`downgrade` symmetric.
Moves 1–3 are mechanically independent and low-risk; Move 4 is the largest
(recall-code surgery); Move 5 is independent of 1–4 and is sequenced last to
keep the chain simple.

## Amendments to memory.md and memory-completion-cutover.md

Each move that removes a spec-mandated table amends the spec in the same PR:

- **Move 1** — `memory.md`: remove `memory_deletions` from "Required Canonical
  Tables"; update "Canonical Records", "Required Lifecycles", and "Privacy And
  Consent" to state deletion/redaction records live in `memory_versions`.
- **Move 2** — `memory.md`: remove `memory_reviews` from "Required Canonical
  Tables"; revise the Layer 5 "Review and conflict control plane" description —
  review history is `memory_versions` + `memory_events`, review state is
  `memory_assertions.lifecycle_state`.
- **Move 3** — `memory.md`: remove `memory_salience` from "Required Canonical
  Tables"; revise Canonical Layer 6 — salience is columns on `memory_assertions`,
  not a table.
- **Move 4** — `memory.md`: remove `memory_reasoning_traces` and
  `memory_action_traces` from "Required Canonical Tables"; collapse the episodic
  and reasoning/action "Memory Types" and Canonical Layer 4 entries to one
  episode concept with an `episode_class`. Update the `memory-completion-cutover.md`
  WS-4/WS-5/WS-7 acceptance criteria.
- **Move 5** — `memory.md`: remove `memory_embedding_projections` from "Required
  Projection Tables"; note the embedding is a column on `memory_assertions`.

Separately, correct the stale prose in `memory-completion-cutover.md` (the
obsolete 7-value `projection_kind` set and the non-existent
`process_unsupported_memory_projection_job`) — the shipped set is
`embedding`/`graph`/`hot_index`.

## Acceptance criteria

- 31 → 25 `memory_*` tables; `MemoryDeletionRecord`, `MemoryReviewRecord`,
  `MemorySalienceRecord`, `MemoryReasoningTraceRecord`, `MemoryActionTraceRecord`,
  and `MemoryEmbeddingProjectionRecord` are gone from `persistence.py`.
- `GET /v1/memory` returns the same `deletions` shape, sourced from
  `memory_versions`.
- Recall returns every kind — assertion, episode, reasoning trace, action trace,
  procedure, project state — with `kind` values unchanged.
- Vector recall works against the `memory_assertions.embedding` column.
- `db.py` `REQUIRED_TABLES` and the constraint registries match the new schema.
- Each migration runs up and down; `ruff`, `mypy`, and the full `pytest` suite
  are green.
- `memory.md` and `memory-completion-cutover.md` carry the amendments above.

## Open forks

- **`task_summary`** (Move 4): JSONB key vs. a real nullable column. Recommended
  JSONB — it is a payload field with no independent lifecycle.
- **`salience_score` / `salience_signals`** (Move 3): folded in for a mechanical
  cutover; a follow-up may drop them as speculative surface.
- **`memory_projection_jobs`**: kept as-is for Phase 2; its fold into
  `background_tasks` is a Phase 4 decision.
