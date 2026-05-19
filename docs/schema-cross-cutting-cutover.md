# Schema Cross-Cutting Cutover

## Scope

Phase 4 — the final phase — of the schema consolidation
([schema-consolidation-cutover.md](schema-consolidation-cutover.md)). It covers
the consolidations that span subsystems: the job-queue duplication, the email
write-ledger duplication, the speculative `workspace_items` generality, and the
family of six `*_events` audit tables.

A cross-cutting deep-dive produced one consequential finding: **the
six-event-table merge the master plan anticipated cannot be done.**
`docs/database.md` mandates foreign keys (`ondelete=RESTRICT`) and
`CheckConstraint` enums. A single polymorphic event table cannot carry the six
typed parent foreign keys (two of the tables carry a second FK as well) and
cannot enforce six disjoint `event_type` enums; JSONB-on-parent cannot carry
foreign keys, dedupe uniqueness, or per-row CHECKs. The event logs stay as six
tables.

That finding, with the Phase 2 and Phase 3 deep-dives — each of which found its
subsystem less consolidatable than the breadth survey estimated — revises the
end state. The honest projected final count is **~72 tables, not the ~60-65 the
master plan first estimated.** The original figure was set before any deep-dive;
every deep-dive came back more conservative because, table by table, most of the
schema earns its place. 86 tables is large, but after a rigorous audit it is
roughly 85% justified. Phase 4 consolidates 2-3 tables.

## Cutover Policy

Inherits [schema-consolidation-cutover.md](schema-consolidation-cutover.md). Each
move is one PR — one Alembic migration, the `persistence.py` model change, the
code change. No compatibility shim, no dual-write, no feature flag. Ariel holds
no production data; migrations drop and recreate tables freely. Every migration
has a working `downgrade()`. Each PR runs `ruff`, `mypy`, and the full `pytest`
suite to green, and tests the migration up and down.

## Moves

| # | Move | Tables |
|---|---|---|
| 1 | Fold `memory_projection_jobs` into `background_tasks` | −1 |
| 2 | Reconcile `email_actions` into `provider_write_receipts` | −1 |
| 3 | Narrow `workspace_items` / `workspace_item_events` to Discord-only | 0 |

### Move 1 — Fold `memory_projection_jobs` into `background_tasks`

`memory_projection_jobs` and `background_tasks` are the same machine — a
PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED` durable work queue — built twice
with renamed columns and a duplicated reaper. Both carry `attempts`, a retry
cap, `error`, `claimed_by`, `run_after`, `last_heartbeat`, and the identical
`pending/running/completed/failed/dead_letter` lifecycle. `docs/coordination.md`
documents exactly one queue pattern, named `background_tasks`. `jobs` is **not**
a queue (it mirrors external Agency state, has no claim columns) — it stays
separate.

Schema: projection jobs become three `task_type` values on `background_tasks` —
`memory_projection_embedding`, `memory_projection_graph`,
`memory_projection_hot_index` — added to `ck_background_task_type`. `target_table`
and `target_id` move into the existing `payload` JSONB. Add one new column,
`attempt_token` `String(32)` nullable — the projection workers process across
multiple transactions and fence re-claims with this token; it is a strict
correctness improvement and should be set on claim for all task types. Drop
`memory_projection_jobs` and its CHECKs.

Code: in `worker.py`, delete `reap_stale_memory_projection_jobs` and
`_mark_memory_projection_job_failed`; the three bespoke projection drains in
`process_one_task` collapse into three `task_type` case arms, and the per-handler
claim/dead-letter boilerplate deletes — `claim_next_task` and the generic failure
path replace it. The three enqueue sites in `memory.py` switch to
`enqueue_background_task`. `projection_health` (`memory.py`) and the eval metric
re-express their failed/dead-letter counts as queries filtered by
`task_type IN (the three projection types)` — a module constant. `db.py` drops
`memory_projection_jobs` from `REQUIRED_TABLES` and its constraint registries and
adds `attempt_token` to the `background_tasks` expectations.

Tests: `tests/integration/test_worker_memory_jobs.py` is the main file — the
projection-job seed helper and assertions move from `MemoryProjectionJobRecord` /
`lifecycle_state` / `projection_kind` to `BackgroundTaskRecord` / `status` /
`task_type`. The `db.py`-drift acceptance tests update their table/constraint
expectations.

Open forks: add a partial unique index on `payload->>'scope_key'`
`WHERE task_type='memory_projection_hot_index'` to make hot-index dedup
race-proof (recommended — mirrors the existing follow-up partial-unique index);
the projection cleanup queries that filtered on the old indexed `target_id` lose
that index when it moves to `payload` — accept the scan (the queue stays small)
or add a JSONB expression index.

### Move 2 — Reconcile `email_actions` into `provider_write_receipts`

Every email mutation (`cap.email.archive/trash/labels.modify/undo`) writes a row
to **both** `email_actions` and `provider_write_receipts`, under two different
idempotency keys derived from the same inputs. Worse, the crash-recovery path
keys on the receipt and never consults `email_actions`, so a crashed email
mutation is reconciled against the wrong ledger — a latent split-brain defect the
merge removes.

`provider_write_receipts` is the broader, doc-sanctioned write ledger
(`docs/modules/google-workspace-reasoning-cutover.md` names it the write ledger
and never mentions `email_actions`). `email_actions` uniquely owns the email-undo
state — `before_state`/`after_state`, `undo_token_hash`, `undo_expires_at` — and
that state is created and finalized with the receipt; it has no independent
lifecycle. It belongs on the receipt.

Schema: add to `ProviderWriteReceiptRecord` (email-only, nullable) — `before_state`
`JSONB`, `after_state` `JSONB`, `undo_token_hash` `String(64)`, `undo_expires_at`
`DateTime(tz)`; add the `status` value `undone`; add a unique partial index on
`undo_token_hash WHERE undo_token_hash IS NOT NULL`; add named CHECKs that gate
the undo fields to email mutation capabilities so non-email rows are
unconstrained. Drop `email_actions` and its indexes — including the two GIN
indexes on message/thread ids, which have no reader. Delete `EmailActionRecord`.

Code (`action_runtime.py`): delete the separate email-mutation dispatch branch —
email mutations fall into the existing Google-write branch, which already writes
an `executing` receipt at dispatch (this is what fixes the crash-recovery bug).
The email provider-call wrapper (before-state pre-fetch, advisory lock) is kept
but reads and updates the receipt. `cap.email.undo` looks up the prior receipt by
`undo_token_hash`. Delete `_email_idempotency_key`; email writes use
`_provider_write_idempotency_key` (email mutations always carry a client
idempotency key, so there is no fallback-path regression).

API: `/v1/email/actions` and `/v1/email/actions/{id}` re-query
`ProviderWriteReceiptRecord` filtered to the email capabilities;
`serialize_email_action` retargets to the receipt and `SurfaceEmailActionContract`
keeps its shape — the external surface is unchanged. `db.py` drops `email_actions`.

Tests: `test_email_decluttering_action_runtime.py` and `test_email_decluttering_api.py`
re-point `EmailActionRecord` assertions to `ProviderWriteReceiptRecord` and drop
the "two rows" assertions. Add a regression test for the crash-recovery case.

### Move 3 — Narrow `workspace_items` / `workspace_item_events` to Discord-only

`workspace_items` advertises a generic multi-provider store
(`provider IN ('google','ariel','discord')`, five `item_type` values) but the
only writer in `src/` writes `provider="discord"`, `item_type="discord_message"`
— always. Google content lives in `google_provider_objects` / `provider_evidence`.
The `google`/`ariel` providers and the calendar/email/drive/internal item types
are unconstructible — speculative surface `docs/simplicity.md` forbids. This move
removes no table; it removes dead generality.

Schema: rename `workspace_items` → `discord_messages` and `workspace_item_events`
→ `discord_message_events`. Drop the `provider` and `item_type` columns and their
CHECKs. Rename `external_id` → `message_id` and make it directly `UNIQUE`
(replacing the 3-column unique). Drop the `(provider, item_type, …)` index.
Rename `proactive_observations.workspace_item_id` → `discord_message_id` and the
`ck_proactive_observation_source_type` value `workspace_item` → `discord_message`
(the CHECK and the writing code change together, or the insert fails).

Code: the writer (`app.py`) drops the two hardcoded kwargs and the
`provider`/`item_type` payload keys; the `proactivity.py` ambient-candidate join
drops the `provider`/`item_type` reads and hardcodes `discord`. The
`/v1/workspace-items` route and its `SurfaceWorkspaceItem*` contracts are renamed
to `discord-messages`; the dead `provider`/`item_type` query parameters are
removed. `db.py` updates the table names.

Tests: `test_proactive_ambient_sources.py` has a fixture that synthesizes a
`provider="google"` workspace item — rewrite it to a Discord message (or drop the
case if it only existed to exercise the now-dead generality). `test_pr01_acceptance.py`
and others update table/route names.

Open fork: `workspace_item_events` only ever receives `event_type="created"`
rows; its 4-value enum is lifecycle headroom. Narrow it to `created` only, or
keep the enum — minor.

## Kept — the event-log family

`job_events`, `memory_events`, `proactive_case_events`, `google_connector_events`,
`workspace_item_events` (renamed in Move 3), and `work_follow_up_events` stay as
six tables. They look uniform but are not: six disjoint `event_type` value
spaces, six distinct typed parent FKs (`job_events` and `workspace_item_events`
carry a second FK), and per-table extras (`memory_events` has `scope_key`/
`actor_id`/`entry_path`; `workspace_item_events` has a unique `dedupe_key`;
`work_follow_up_events` has `loop_version`). A shared polymorphic table cannot
hold typed FKs or per-`entity_type` CHECK enums — `docs/database.md` mandates
both. JSONB-on-parent fails the same doc's JSONB rule: event logs have an
independent lifecycle (each row independently inserted, ordered, range-filtered,
paginated through HTTP contracts), grow unbounded, and cannot carry FKs or the
`dedupe_key` uniqueness. No code queries across more than one event table — a
shared table would serve a query pattern that does not exist. Keep all six.

Open fork — `work_follow_up_events` is write-only (14 writers, no reader). That
is the profile of `memory_reviews`, which Phase 2 dropped. Decide explicitly:
drop it if its signal is reconstructable from `work_follow_up_loops` +
`background_tasks`, or keep it as a deliberate audit log. If dropped, Phase 4
removes a third table.

## Kept — `proactive_feedback`

Phase 3 deferred `proactive_feedback` to this phase. Verdict: **keep it as a
typed entity.** It is not an audit event — it has a promoted `note` column and a
user-intent `feedback_type` enum, it is the parent of the learning chain
(`proactive_learning_records.feedback_id` FKs it), and the async learning worker
row-locks and re-reads it. The audit concern is already separately owned by the
`feedback_recorded` row in `proactive_case_events`. Folding feedback into the
event log would force the learning FK onto a polymorphic event row. Keep both
tables.

## Kept — confirmed by the end-state audit

Three side/satellite tables that an end-state audit flags were each examined in
the Phase-1 survey and earn their place; recorded here so the consolidation has a
complete accounting:

- `action_private_payloads` — a deliberate privacy/redaction boundary: encrypted
  sensitive action input, key-versioned, separately hard-deletable without
  touching the queryable `action_attempts` audit row. Keep.
- `turn_idempotency_keys` — caches a full HTTP response keyed by idempotency key,
  including for requests that failed before a `turns` row existed; a column on
  `turns` cannot represent that. `docs/operation-types.md` blesses a dedicated
  idempotency table. Keep.
- `provider_evidence_blocks` — evidence blocks are cited individually by id (an
  AI extraction must reference real block ids); a JSONB array cannot be a
  foreign-key target. Keep.

## Sequencing

Three independent PRs, a linear Alembic chain — Move 1, then Move 2, then Move 3.
None depends on another. All of Phase 4 runs after Phases 2-3.

## Acceptance criteria

- `memory_projection_jobs` and `email_actions` are gone from `persistence.py`;
  `workspace_items`/`workspace_item_events` are renamed and narrowed.
- The memory projection pipeline runs on `background_tasks`; `projection_health`
  reports the same numbers via the filtered query.
- Email writes go to one ledger; the crash-recovery path reconciles it correctly.
- `db.py` `REQUIRED_TABLES` and constraint registries match the new schema.
- Each migration runs up and down; `ruff`, `mypy`, and the full `pytest` suite
  are green.

## End state

After Phase 4: **~72 `memory_*` + non-memory application tables** (71 if
`work_follow_up_events` is also dropped) — down from 86. The consolidation
removed dead schema (`work_people`, `connector_subscriptions`, `memory_reviews`),
fully-derivable tables (`memory_deletions`), 1:1 satellites
(`memory_salience`, `proactive_context_snapshots`, `proactive_policy_validations`,
`memory_embedding_projections`), near-duplicate tables (the three memory trace
tables, `proactive_turns`, `memory_projection_jobs`, `email_actions`), and
duplicate AI-call storage. What remains is, table by table, justified.

## Open forks

- `work_follow_up_events`: drop (write-only, `memory_reviews` precedent) or keep
  as deliberate audit.
- Move 1: partial unique index for hot-index dedup; `attempt_token` for all task
  types vs. projection-only.
- Move 3: narrow `event_type` to `created` only, or keep the enum.
