# AI-Judgments Cutover

## Scope

This cutover finishes `ai_judgments` now that the proactivity crystallisation
(`proactivity-cutover.md`) has landed. In the post-crystallisation codebase
`ai_judgments` is a **standalone, purely write-only audit log**: `proactivity.py`
and `leave_by.py` are deleted, `proactive_decisions` — the one table that held a
foreign key into `ai_judgments` — is dropped, and all three former readers (two
dedup guards, one feedback lookup) went with `proactivity.py`. The only
remaining writers are `memory.py` (`memory_recall`, `memory_remember`) and
`app.py`'s turn loop (`model_output`).

This cutover does three things and nothing else:

1. **Trim** the table from 22 columns to 16 — drop `selected`, `omitted`,
   `rationale`, `uncertainty`, `confidence`, `updated_at`, and the index
   `ix_ai_judgments_updated_at`.
2. **Narrow two CHECK enums to the live set** — `ck_ai_judgment_type` from nine
   values to three (`memory_recall`, `memory_remember`, `model_output`), and
   `ck_ai_judgment_parse_status` from five to four (drop `not_required_no_candidates`).
3. **Consolidate the writer** — replace `memory._write_judgment` and
   `app.add_ai_judgment` with one owner: `record_ai_judgment` in a new
   `src/ariel/ai_judgments.py`.

It removes no table and changes no runtime behaviour. The
`SurfaceEventAIJudgmentPayloadContract` `Literal`s and the `db.py`
schema-verification constants are narrowed in lockstep with the CHECK enums.

## Background

`ai_judgments` is the cross-subsystem audit log of bounded AI subagent calls —
one row per call, mandated by `../ai-first.md`. It is sound infrastructure and
stays. It had two faults this cutover fixes, plus dead enum surface the
crystallisation left behind.

- **Six columns earn no place.** `selected`, `omitted`, `rationale`,
  `uncertainty`, and `confidence` were written by producers and read by no code.
  `updated_at` is dead by construction: the table is append-only (no code
  mutates an `AIJudgmentRecord` after insert), so `updated_at` always equalled
  `created_at`, and it carried its own redundant index.
- **Two bespoke writers.** The pre-crystallisation sprawl — five writer helpers
  and ten open-coded `AIJudgmentRecord(...)` sites — was mostly proactivity; the
  crystallisation deleted three helpers and ~13 sites with `proactivity.py` and
  `leave_by.py`. What was left was still a `../simplicity.md` violation: one
  capability, "write a judgment row", in two forms — `memory._write_judgment`
  and `app.add_ai_judgment` — that populated the audit columns differently.
- **Dead enum values.** `tool_result_interpretation` lost its producer in commit
  `b026bfa` ("cut over to the sandboxed Python-program run model"). The
  crystallisation removed the producers of `feedback_learning`,
  `ambient_interpretation`, `proactive_deliberation`, `workspace_commitment_extraction`,
  and `leave_by_evaluation` without amending the `ck_ai_judgment_type` CHECK —
  leaving it advertising six values nothing can write.

The crystallisation removed the bulk; this cutover finishes `ai_judgments` itself.

## Prerequisite — the proactivity crystallisation has landed

This cutover is sequenced **after** the entire proactivity crystallisation
(`proactivity-cutover.md`, phases P1–P5), which is merged to `main` as `a374949`.
`proactivity.py` and `leave_by.py` are deleted and `proactive_decisions` is
dropped, so `ai_judgments` has exactly two writers and no readers. The
dependency was hard: the proactivity and leave-by writers set the six columns
this cutover drops and produced the six judgment types it retires, so the
cutover could not have been correct or green before P4.

## Cutover policy

Inherits `../schema-consolidation-cutover.md` and `proactive-consolidation-cutover.md`.
One PR: one Alembic migration, the `persistence.py` model change, the new
`ai_judgments.py` module, the writer conversions, the `db.py` and contract
adjustments. No compatibility shim, no dual-write, no feature flag, no fallback,
no legacy branch — a hard cutover. The migration has a working `downgrade()`.
`ruff`, `mypy`, and the full `pytest` suite are green; the migration is tested
up and down.

## Goals

- `ai_judgments` carries only columns with a load-bearing audit role. The trim
  is information-lossless.
- The two CHECK enums advertise exactly the values the one writer can emit —
  the DB constraint and the writer are provably aligned.
- Exactly one writer. No code constructs `AIJudgmentRecord` outside
  `ai_judgments.py`.
- The four outcome columns — `status`, `parse_status`, `validation_status`,
  `failure_code` — are derived inside the writer from one argument. A caller
  cannot record an inconsistent 4-tuple.
- A new subagent adds a `judgment_type` (one CHECK value, one `Literal` member),
  not a writer.

## Non-goals

- **Retention.** `ai_judgments` is append-only with no pruning, and after this
  cutover it is purely write-only. A retention policy is operational (a
  recurring job plus a policy decision), not a structural refactor; it ships
  separately so this cutover stays purely subtractive.
- **The `ck_ai_judgment_failure_code` / `ck_ai_judgment_status` /
  `ck_ai_judgment_validation_status` enums.** Every value of each remains
  reachable through the writer; they are unchanged.
- **A fuller audit of `SurfaceEventAIJudgmentPayloadContract`.** This cutover
  touches only the fields tied to the dead judgment concepts.
- **An eval/quality reader.** The columns are trimmed, not retained pending a
  hypothetical consumer. One writer makes it cheap to add back exactly what such
  a reader would need, later.

## Final state — the table

`ai_judgments`, 16 columns (was 22), no foreign keys in or out, no code readers:

| Column | Type | Notes |
|---|---|---|
| `id` | `String(32)` PK | client-generated, `ajg_…` |
| `judgment_type` | `String(64)`, indexed | CHECK `ck_ai_judgment_type` — 3 values |
| `source_type` | `String(64)`, indexed | provenance |
| `source_id` | `String(128)`, indexed | provenance |
| `status` | `String(32)` | CHECK; writer-derived |
| `model` | `String(128)` nullable | model id, or NULL |
| `prompt_version` | `String(64)` | versioned prompt constant |
| `provider_response_id` | `String(128)` nullable | for replay / provider support |
| `input_summary` | `Text` | human-readable input description |
| `input_refs` | `JSONB` | structured input references |
| `output` | `JSONB` | the model's output — the audit payload |
| `parse_status` | `String(32)` | CHECK — 4 values; writer-derived |
| `validation_status` | `String(32)` | CHECK; writer-derived |
| `failure_code` | `String(64)` nullable | CHECK; writer-derived |
| `failure_reason` | `Text` nullable | human-readable failure detail |
| `created_at` | `DateTime(tz)`, indexed | write timestamp |

Indexes: `judgment_type`, `source_type`, `source_id`, `created_at` (4; was 5).

CHECK constraints after the cutover:

- `ck_ai_judgment_type`: `judgment_type IN ('memory_recall', 'memory_remember', 'model_output')`
- `ck_ai_judgment_parse_status`: `parse_status IN ('parsed', 'invalid_json', 'missing_output', 'schema_invalid')`
- `ck_ai_judgment_status`, `ck_ai_judgment_validation_status`,
  `ck_ai_judgment_failure_code` — unchanged.

## Final state — the writer

One module, `src/ariel/ai_judgments.py`, owns everything write-side: the
`AIJudgmentRecord` constructor call, `record_ai_judgment`, and the
`AIJudgmentFailure` type. Both producers — `memory.py` and `app.py` — call
`record_ai_judgment`. `memory._write_judgment` and `app.add_ai_judgment` are
deleted. `grep -rn 'AIJudgmentRecord(' src/` returns exactly one hit, inside
`ai_judgments.py`.

## Architecture

`ai_judgments.py` imports `AIJudgmentRecord` from `persistence.py` and is
imported by `memory.py` and `app.py` — no import cycle:

```
persistence.py
   ↑
ai_judgments.py
   ↑
memory.py · app.py
```

It is a small module, and that is correct: the owner of "write an `ai_judgments`
row" is neither `memory` nor `app`. Putting the writer in `memory.py` and
importing it into `app.py` would be the cross-module private-helper import that
`../cleanliness.md` says to fix by moving code to its owner. `persistence.py`
owns ORM models, not logic.

`AIJudgmentFailure` (a `RuntimeError` subclass) moves from `memory.py` to
`ai_judgments.py` — it is a judgment concept, not a memory concept, and `app.py`
needs it too. `memory.py` imports it back; its `_schema_failure` /
`_validation_failure` / `_call_subagent` still construct and raise it unchanged.

## Capability contract & API design

The one writer:

```python
def record_ai_judgment(
    db: Session,
    *,
    judgment_type: Literal["memory_recall", "memory_remember", "model_output"],
    source_type: str,
    source_id: str,
    model: str | None,
    prompt_version: str,
    provider_response_id: str | None,
    input_summary: str,
    input_refs: Mapping[str, Any],
    output: Mapping[str, Any],
    now: datetime,
    new_id: Callable[[str], str],
    failure: AIJudgmentFailure | None = None,
) -> None:
```

Success versus failure is the single `failure` argument — a two-state
discriminated input, discriminant `is None`:

| `failure` | `status` | `parse_status` | `validation_status` | `failure_code` | `failure_reason` |
|---|---|---|---|---|---|
| `None` | succeeded | parsed | valid | NULL | NULL |
| set | failed | `failure.parse_status` | `failure.validation_status` | `failure.code` | `failure.safe_reason` |

The four outcome columns are derived **inside the writer** — no caller assembles
them, so an inconsistent 4-tuple is unrepresentable. The writer also runs
`input_refs` and `output` through `jsonable_encoder`, mints the id with
`new_id("ajg")`, sets `created_at=now`, and `db.add()`s the row to the caller's
transaction (no flush, no commit).

`AIJudgmentFailure` is the failure descriptor — a `RuntimeError` subclass
carrying `code`, `safe_reason`, `retryable`, `parse_status`, `validation_status`,
`provider_response_id`. `memory`'s subagents raise it (they fail closed);
`app.py` constructs one for each of its three `model_output` failure cases.

**Why `failure: AIJudgmentFailure | None` and not a discriminated
`AIJudgmentOutcome` union.** An earlier draft of this spec proposed a four-variant
union (`Succeeded` / `ModelCallFailed` / `OutputUnparseable` / `OutputRejected`).
Post-crystallisation there are exactly two callers, and `AIJudgmentFailure`
already exists as a correct-by-construction failure descriptor (built by
`_schema_failure` / `_validation_failure` / `_call_subagent`). A four-dataclass
union would re-encode what `AIJudgmentFailure` already carries — `../simplicity.md`:
"prefer using [an existing capability] over introducing a near-duplicate", and
do not introduce data shapes that do not earn their place. `None | AIJudgmentFailure`
delivers the same guarantee — the outcome columns derived in one place — with
zero new types.

### Failure construction

- **memory** — `_call_subagent` raises `AIJudgmentFailure` for provider errors
  (`parse_status="missing_output"`) and malformed JSON (`invalid_json`);
  `_schema_failure` raises `schema_invalid`; `_validation_failure` raises
  `parsed`/`invalid`. `run_retriever` / `run_rememberer` catch it and pass it to
  `record_ai_judgment` before re-raising.
- **app `model_output`** — budget exhaustion →
  `AIJudgmentFailure(code="E_AI_JUDGMENT_BUDGET", parse_status="missing_output",
  validation_status="not_validated")`; run-protocol and program-execution
  failures → `AIJudgmentFailure(code="E_AI_JUDGMENT_VALIDATION",
  parse_status="parsed", validation_status="invalid")`.

The writer never emits `not_required_no_candidates` (success → `parsed`; failure
→ a `failure.parse_status` that is never that value), so
`ck_ai_judgment_parse_status` narrows to its four reachable values. Because the
writer is the sole producer of the outcome columns, the CHECK constraints can be
tightened to exactly what it emits — a narrowing that was unsafe under the old
scattered writers.

## The trim — column by column

The trim is **information-lossless**.

- **`updated_at`** — dead by construction. `ai_judgments` is append-only;
  `updated_at` was always equal to `created_at`. Column and
  `ix_ai_judgments_updated_at` dropped.
- **`selected`** — held the fact ids a memory call selected or touched. `app`
  never set it. To keep the audit row complete, `memory`'s two success calls now
  fold those ids into `output` — `fact_ids` for the retriever,
  `touched_fact_ids` for the rememberer.
- **`omitted` / `rationale` / `uncertainty` / `confidence`** — `memory` always
  wrote `[]` / `None`; `app` never set them. No data is lost.

## The enum narrowing

`ck_ai_judgment_type` drops to `('memory_recall', 'memory_remember', 'model_output')`.
The other six values have no producer — `tool_result_interpretation` since
`b026bfa`, the rest with `proactivity.py` and `leave_by.py`.
`ck_ai_judgment_parse_status` drops `not_required_no_candidates`. `../cleanliness.md`:
delete surface "kept only for … storage formats that no longer exist."

Narrowing a CHECK requires no surviving row to violate it. The migration purges
dead-`judgment_type` rows first (`DELETE FROM ai_judgments WHERE judgment_type
NOT IN (…)`, the pattern four earlier migrations used); that purge also clears
every `not_required_no_candidates` row, since those were all `ambient_interpretation`.

## Files

- **`src/ariel/ai_judgments.py`** — new. `record_ai_judgment` and
  `AIJudgmentFailure` (moved from `memory.py`).
- **`src/ariel/persistence.py`** — `AIJudgmentRecord`: six `mapped_column`s
  removed; `ck_ai_judgment_type` and `ck_ai_judgment_parse_status` expressions
  narrowed; the now-unused `Float` import dropped.
- **`alembic/versions/20260519_0049_trim_ai_judgments.py`** — new. `down_revision`
  `20260518_0048`. `upgrade()`: purge dead-`judgment_type` rows; drop
  `ix_ai_judgments_updated_at` and the six columns; drop and recreate the two
  CHECK constraints. `downgrade()`: recreate the wide constraints, re-add the
  six columns (NOT NULL ones via a `server_default` then cleared) and the index.
- **`src/ariel/db.py`** — schema-verification constants: `REQUIRED_COLUMNS`
  drops `confidence`/`updated_at`; `REQUIRED_CHECK_SQL_FRAGMENTS` for
  `ck_ai_judgment_type` becomes the three live values;
  `FORBIDDEN_CHECK_SQL_FRAGMENTS` gains the six retired types (guarding re-entry,
  matching the existing `tool_strategy` entry).
- **`src/ariel/memory.py`** — `_write_judgment` and the `AIJudgmentFailure`
  class definition deleted; `record_ai_judgment` / `AIJudgmentFailure` imported
  from `ai_judgments`; the four call sites converted; the selected/touched fact
  ids folded into `output`.
- **`src/ariel/app.py`** — the nested `add_ai_judgment` helper deleted; the four
  `model_output` call sites converted to `record_ai_judgment`, the three failure
  sites constructing an `AIJudgmentFailure`.
- **`src/ariel/response_contracts.py`** — `SurfaceEventAIJudgmentPayloadContract`:
  `judgment_type` `Literal` narrowed to the three live values, `parse_status`
  `Literal` drops `not_required_no_candidates`, the dead
  `last_tool_result_interpreter_judgment_id` field removed.
- **`tests/integration/test_memory.py`** — the `_judgment_row` helper drops the
  six removed columns; the `ck_ai_judgment_type` test updated to the three live
  / three retired types and renamed.

## Acceptance criteria

All met:

- `ai_judgments` has 16 columns; the six columns and `ix_ai_judgments_updated_at`
  are gone. `ck_ai_judgment_type` has three values; `ck_ai_judgment_parse_status`
  has four. The other three CHECK constraints and the absence of foreign keys
  are unchanged.
- `grep -rn 'AIJudgmentRecord(' src/` returns exactly one constructor, in
  `ai_judgments.py`. `_write_judgment` and `add_ai_judgment` no longer exist.
- `status`, `parse_status`, `validation_status`, `failure_code` are assigned
  only inside `record_ai_judgment`.
- `db.py` and `SurfaceEventAIJudgmentPayloadContract` advertise only the live
  judgment types and parse statuses.
- The migration runs up and down cleanly.
- `ruff`, `mypy` (`src` + `tests`), and the full `pytest` suite are green.

## Key decisions

1. **`output` stays, though no code reads `ai_judgments` post-crystallisation.**
   It is the irreducible payload of an AI-call audit log; its readers are
   humans, SQL, and future eval tooling — the normal consumers of an audit log.
2. **The trim is lossless** — every dropped column is dead (`updated_at`) or
   carried no data not also in `output` (the fact ids are folded in).
3. **The CHECK enums are narrowed to the writer's emit set.** One writer makes
   the DB constraint and the code provably consistent.
4. **New module, not an extension of `persistence.py`.** One concern, one owner;
   `persistence.py` owns ORM models, not logic.
5. **The writer derives the four outcome columns** from the `failure` argument;
   callers never pass them — inconsistency is unrepresentable.
6. **`failure: AIJudgmentFailure | None`, no `AIJudgmentOutcome` union.** With
   two callers and an existing failure descriptor, a four-dataclass union would
   be a near-duplicate the minimalism rules reject.
7. **`AIJudgmentFailure` moves to `ai_judgments.py`.**
8. **Retention is deferred** — operational, not structural.

## Open forks & risks

- **Retention** remains unaddressed. `ai_judgments` is now a purely write-only
  table that grows without bound — a small but real follow-up, more pointed now
  that nothing reads the table.
