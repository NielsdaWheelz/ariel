# s3 pr-01 implementation notes

## delivered scope

- added `cap.search.web` as a capability-registry `read` action with:
  - strict `query` schema validation
  - explicit egress intent declaration
  - allowlisted egress destination policy
  - provider-agnostic capability contract boundary
- generalized runtime egress preflight to enforce declared intent + allowlist for any capability that
  declares outbound intent.
- added retrieval-specific grounded synthesis in action runtime:
  - inline citation markers in assistant text
  - synchronized `assistant.sources[]` entries with artifact ids
  - uncertainty + recovery guidance when evidence is missing
  - partial-result labeling + recovery guidance when some retrieval steps fail
- added durable retrieval provenance artifacts (`artifacts` table + serializer + endpoint):
  - `GET /v1/artifacts/{artifact_id}`
- expanded surfaced response contracts:
  - `assistant.sources[]` (message + approval responses; empty list when no citations)
  - `surface_artifact_response`

## persistence model

new table: `artifacts`

- `id` (`art_*`), `session_id`, `turn_id`, `action_attempt_id`
- `artifact_type` (`retrieval_provenance`)
- `title`, `source`, `snippet`
- `retrieved_at`, `published_at`, `created_at`, `updated_at`

notes:

- pr-01 surfaces only allowlisted artifact metadata (`id`, `type`, `title`, `source`, `retrieved_at`,
  `published_at`).
- runtime-only/internal fields remain unsurfaced.

## runtime behavior notes (historical)

- at pr-01 merge time, retrieval synthesis applied only for retrieval-only proposal sets
  (`cap.search.web` proposals only).
- at pr-01 merge time, mixed proposal sets (retrieval + non-retrieval) kept standard action appendix
  behavior so non-retrieval action outcomes were preserved.
- superseded in pr-03: retrieval-backed mixed turns now keep grounded narrative in `assistant.message`
  with telemetry retained in structured lifecycle/event surfaces.
- retrieval citations are capped and persisted with stable identities.

## config

- `ARIEL_SEARCH_WEB_API_KEY` (required for live backend)
- `ARIEL_SEARCH_WEB_ENDPOINT` (optional)
- `ARIEL_SEARCH_WEB_TIMEOUT_SECONDS` (optional)

## verification run for this implementation

- `pytest tests/integration/test_s3_pr01_acceptance.py`
- `pytest tests/integration/test_s2_pr08_acceptance.py tests/integration/test_s2_pr06_acceptance.py`
- `make verify`
- `make e2e`
