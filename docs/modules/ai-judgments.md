# AI Judgments

## Role

`ai_judgments` is the append-only audit log for bounded AI model calls. It
records what the model saw, what it returned, and whether parsing or validation
failed. Runtime behavior must not depend on reading this table.

## Owner

`src/ariel/ai_judgments.py` is the only writer. It owns
`record_ai_judgment`, `AIJudgmentFailure`, and the single
`AIJudgmentRecord(...)` constructor call.

`persistence.py` owns the ORM model. Alembic migrations own schema changes.
Callers own the model-call context and pass one outcome to `record_ai_judgment`;
the writer derives `status`, `parse_status`, `validation_status`, and
`failure_code`.

## Judgment Types

The live `judgment_type` values are:

- `memory_recall`
- `memory_encode`
- `memory_dream`
- `model_output`

Memory calls are described in [memory.md](memory.md). Main-agent model output is
described in [agent-loop.md](agent-loop.md).

## Rules

- Construct `AIJudgmentRecord` only in `ai_judgments.py`.
- Add a new `judgment_type` only with the model-call path that writes it, the
  schema CHECK update, and tests proving the persisted value is accepted.
- Do not add a second writer, dual-write path, compatibility branch, or
  call-site-derived status tuple.
- Do not read `ai_judgments` to make product decisions. It is audit evidence,
  not application state.
- Record safe failure reasons only. Raw provider errors stay behind the
  redaction boundary.
