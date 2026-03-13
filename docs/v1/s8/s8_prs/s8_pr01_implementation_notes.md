# s8 pr-01 implementation notes

## scope delivered

- added first-class `POST /v1/captures` ingress for bounded `text` and `url` payloads with:
  - optional `note`
  - optional `source` metadata (`app`, `title`, `url`)
  - no client-supplied session id
- introduced durable capture persistence via `captures` table:
  - stable `cpt_` identity
  - original capture payload
  - normalized turn input
  - effective session linkage
  - turn linkage
  - durable terminal classification (`turn_created` vs `ingest_failed`)
- reused the same turn execution/orchestration/action lifecycle path as chat turns by extracting
  shared turn execution flow into `_execute_turn_for_session(...)`.
- added strict surfaced capture contracts:
  - success: `{ok, capture, session, turn, assistant}`
  - failure: `{ok, capture, error}`
- implemented capture-scoped idempotency across retries and session rotation:
  - same key + same payload replays prior result
  - same key + different payload returns `409 E_IDEMPOTENCY_KEY_REUSED`
- added durable typed ingest failures for invalid/unsupported/oversize capture input.
- preserved observe-first semantics by normalizing capture input with explicit untrusted-ingress framing,
  so bare capture content does not become an implicit memory/approval side channel.

## scope follow-on note

- pr-02 extended `POST /v1/captures` with `kind="shared_content"` (`shared_content.text?`,
  `shared_content.urls[]`) while preserving pr-01 ingress/idempotency and turn-linkage guarantees.

## key hardening decisions

### idempotency race closure for concurrent same-key capture retries

- added capture-idempotency advisory locking (`_acquire_capture_idempotency_lock`) before idempotency
  lookup/creation to avoid concurrent same-key requests producing a transient 500 race.

### durable failure classification for non-object payloads

- changed capture request parsing to accept any json body shape and classify non-object payloads as
  typed durable ingest failures (`E_CAPTURE_PAYLOAD_INVALID`) instead of generic fastapi
  validation envelopes.

### persistence/index hygiene

- removed redundant non-unique `captures.idempotency_key` index and kept only the partial unique
  index (`ix_captures_idempotency_key_unique`) to avoid duplicate btree maintenance overhead.

## config and contract notes

- capture idempotency is opt-in via `Idempotency-Key` request header.
- when `Idempotency-Key` is omitted, identical capture payloads are treated as distinct submissions.
- authenticated ingress is currently enforced by deployment perimeter controls (private tailnet /
  ingress layer), not per-endpoint in-app auth middleware.

## files changed

- `src/ariel/app.py`
- `src/ariel/persistence.py`
- `src/ariel/response_contracts.py`
- `src/ariel/db.py`
- `alembic/versions/20260313_0008_capture_ingress.py`
- `tests/integration/test_s8_pr01_acceptance.py`
- `README.md`
- `docs/v1/s8/s8_prs/s8_pr01_implementation_notes.md`

## verification

- targeted s8 acceptance:
  - `.venv/bin/python -m pytest tests/integration/test_s8_pr01_acceptance.py`
- full quality gate:
  - `make verify`
- manual cli verification:
  - exercised successful capture create + idempotent replay + conflicting replay + invalid payload
    and confirmed expected `200/200/409/422` behavior with stable capture replay identity.
