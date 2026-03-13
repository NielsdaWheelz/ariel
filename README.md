# ariel

slice-0 walking skeleton with:

- fastapi backend
- postgres-backed sessions/turns/events persistence
- slice-2 action-attempt + approval persistence (`action_attempts`, `approval_requests`)
- single active-session contract
- phone-first web chat surface at `/`
- acceptance-first integration tests using real postgres (testcontainers)

## quickstart

first-time setup (checks prerequisites, creates venv, starts db, runs migrations, configures tailscale):

```bash
make bootstrap
```

if the API key is still the placeholder, edit `.env.local` and re-run `make bootstrap`.

daily development after bootstrap:

```bash
make dev
```

## verification gates

```bash
make verify
make e2e
# or:
bash scripts/agency_verify.sh
```

`make e2e` runs high-signal smoke coverage for the phone surface timeline plus slice-1 bounded-context event auditing.
for maps-focused acceptance during slice-6 pr-02 work, run:

```bash
.venv/bin/python -m pytest tests/integration/test_s6_pr02_acceptance.py
```

for url-extract acceptance during slice-7 pr-01 work, run:

```bash
.venv/bin/python -m pytest tests/integration/test_s7_pr01_acceptance.py
```

for quick-capture acceptance during slice-8 pr-01 work, run:

```bash
.venv/bin/python -m pytest tests/integration/test_s8_pr01_acceptance.py
```

## slice-2 action surface

the action engine now evaluates model proposals per turn and emits an auditable lifecycle:

- `evt.action.proposed`
- `evt.action.policy_decided`
- `evt.action.approval.requested|approved|denied|expired`
- `evt.action.execution.started|succeeded|failed`

user-facing turn/timeline payloads now include a dedicated surfaced lifecycle projection:

- `turn.surface_action_lifecycle[]` with allowlisted, redacted fields only:
  - `proposal` (`capability_id`, `input_summary`)
  - `policy` (`decision`, `reason`)
  - `approval` (`status`, `reference`, `reason`, `expires_at`, `decided_at`)
  - `execution` (`status`, `output`, `error`)

the phone surface renders action details directly from this surfaced projection, not from raw engine records.
`turn.action_attempts` is not part of the user-facing turn/timeline contract.

slice-2 pr-06 locks response boundaries for user-facing slice-2 APIs:

- `POST /v1/sessions/{session_id}/message`, `GET /v1/sessions/{session_id}/events`,
  `POST /v1/approvals`, and `POST /v1/captures` are schema-enforced surfaced contracts.
- message responses expose `assistant.message` only (not `assistant.provider/model`).
- turn events use strict per-`event_type` payload schemas (no open `events[].payload` dictionaries).
- contract drift is fail-closed with `E_RESPONSE_CONTRACT` and sanitized error details.

slice-2 pr-07 closes the deterministic expiry gap for pending approvals:

- `GET /v1/sessions/{session_id}/events` reconciles expired pending approvals to terminal `expired` state.
- reconciliation emits exactly one auditable `evt.action.approval.expired` per reconciled approval.
- repeated timeline reads and post-reconcile approval decisions remain non-executing and idempotent.

slice-2 pr-08 hardens outbound side effects with fail-closed egress preflight:

- `external_send` capabilities declare egress intent via capability contract metadata and runtime
  preflights destinations before dispatch.
- missing/malformed/undeclared/non-allowlisted intent is blocked before external dispatch attempts.
- outbound dispatch flows through a centralized runtime boundary instead of capability-specific
  side-effect paths.

## slice-3 pr-01 grounded retrieval core

slice-3 pr-01 introduces the first externally grounded factual retrieval path with citation and provenance
contracts:

- factual retrieval executes through `cap.search.web` (`read`) under capability policy, not model-vendor
  native search shortcuts.
- grounded message responses now include:
  - inline citation markers in `assistant.message` (for example `[1]`, `[2]`)
  - structured citation metadata in `assistant.sources[]` with stable `artifact_id` values.
- each cited source is persisted as a retrieval provenance artifact and can be inspected via:
  - `GET /v1/artifacts/{artifact_id}`
- retrieval egress is fail-closed with explicit declared intent + destination allowlist preflight.
- retrieval failure modes (timeout/rate-limit/upstream/no evidence) surface explicit uncertainty or
  partial-result recovery guidance in user-visible assistant messages.

slice-3 pr-03 hardens grounding safety for conflicting evidence and mixed proposal sets:

- when any retrieval capability executes (`cap.search.web`, `cap.search.news`, `cap.weather.forecast`, `cap.web.extract`),
  `assistant.message` stays grounded narrative with inline citations and synchronized `assistant.sources[]`,
  even if non-retrieval proposals run in the same turn.
- mixed-turn non-retrieval outcomes remain auditable through structured surfaces
  (`turn.surface_action_lifecycle[]`, `turn.events[]`) instead of raw action-result appendix text.
- unsupported model-authored external assertions are not surfaced when retrieval grounding is present;
  surfaced answer text is derived from citation-backed retrieval synthesis.
- conflicting same-claim retrieval evidence fails closed to uncertainty + concrete recovery guidance.

## slice-4 pr-01 google connector + read flows

slice-4 pr-01 adds google oauth connector lifecycle and allowlisted read capabilities:

- connector lifecycle endpoints:
  - `GET /v1/connectors/google`
  - `GET /v1/connectors/google/events`
  - `POST /v1/connectors/google/start`
  - `POST /v1/connectors/google/reconnect`
  - `GET /v1/connectors/google/callback`
  - `DELETE /v1/connectors/google`
- allowlisted read capabilities:
  - `cap.calendar.list`
  - `cap.calendar.propose_slots`
  - `cap.email.search`
  - `cap.email.read`
- typed recoverable auth/scope failures:
  - `not_connected`, `consent_required`, `scope_missing`, `token_expired`, `access_revoked`

## slice-4 pr-02 approval-safe writes (calendar create + email draft/send)

slice-4 pr-02 extends google workspace write safety with least-privilege scope remediation and
approval-safe execution boundaries:

- new write capabilities:
  - `cap.calendar.create_event` (`write_reversible`, approval required)
  - `cap.email.draft` (`write_reversible`, allowlisted inline, draft-only by construction)
  - `cap.email.send` (`external_send`, approval required)
- reconnect is capability-intent driven:
  - `POST /v1/connectors/google/reconnect?capability_intent=<capability_id>`
  - runtime requests only scopes needed for the attempted write intent while preserving already-granted scopes.
- write-path auth/scope failures stay typed and deterministic:
  - `not_connected`, `consent_required`, `scope_missing`, `token_expired`, `access_revoked`
- draft/send boundary is hard:
  - draft returns canonical local draft state (`drafted_not_sent`, `delivery_state=draft_only`, `sent=false`)
    and never performs external delivery.
  - send remains approval-gated and executes only once per approved payload hash.

## slice-4 pr-03 connector readiness semantics + attendee consent closure

slice-4 pr-03 closes readiness/remediation gaps so connector status semantics match runtime auth outcomes:

- readiness remap is deterministic and explicit:
  - blocking failures (`consent_required`, `scope_missing`, `access_revoked`) surface as
    `readiness=reconnect_required`.
  - transient failures (for example `token_expired`) do not remap a healthy connected connector by
    themselves.
- blocking readiness is sticky until user remediation:
  - reconnect-required state is preserved until successful reconnect callback completion (or explicit
    disconnect).
- attendee slot-planning reconnect is intent-aware and least-privilege:
  - `POST /v1/connectors/google/reconnect?capability_intent=cap.calendar.propose_slots`
    requests `calendar.freebusy` while preserving already granted scopes and avoiding unrelated scope
    escalation.
- slot-planning behavior closes the consent loop:
  - without attendee free/busy consent, outputs stay explicit user-calendar-only fallback with reconnect
    guidance.
  - after attendee consent, outputs use attendee intersection and stop fallback-only guidance.

## slice-5 durable memory + session management hardening

slice-5 adds canonical durable memory, explicit + threshold rotation, and hardened session ingress:

- message idempotency:
  - `POST /v1/sessions/{session_id}/message` accepts optional `Idempotency-Key`.
  - same key + same payload replays the prior turn result.
  - same key + different payload returns `409` with `E_IDEMPOTENCY_KEY_REUSED`.
- rotation surfaces:
  - `POST /v1/sessions/rotate`
  - `GET /v1/sessions/rotations`
  - rotation reason codes: `user_initiated`, `threshold_turn_count`, `threshold_age`, `threshold_context_pressure`.
- timeline incremental sync:
  - `GET /v1/sessions/{session_id}/events?after=<event_id>`
  - unknown cursor returns `404` with `E_EVENT_CURSOR_NOT_FOUND`.
- session lifecycle:
  - surfaced session payloads now include `lifecycle_state` (`active`, `rotating`, `closed`, `recovery_needed`).
- context assembly contract (deterministic order):
  - `policy_system_instructions`
  - `recent_active_session_turns`
  - `rolling_session_summary`
  - `durable_memory_recall`
  - `open_commitments_and_jobs`
  - `relevant_artifacts_and_signals`

## slice-6 pr-01 drive vertical (search/read/share)

slice-6 pr-01 adds drive capabilities and capability-scoped reconnect intent under existing policy and
approval boundaries:

- allowlisted read capabilities:
  - `cap.drive.search` (metadata-oriented file discovery)
  - `cap.drive.read` (bounded content retrieval with typed read outcomes)
- approval-gated external-send capability:
  - `cap.drive.share` (`requires_approval`, exact approved payload, exactly-once execution)
- reconnect intent remains least-privilege by capability:
  - `POST /v1/connectors/google/reconnect?capability_intent=cap.drive.search`
  - `POST /v1/connectors/google/reconnect?capability_intent=cap.drive.read`
  - `POST /v1/connectors/google/reconnect?capability_intent=cap.drive.share`
- drive auth/scope failures remain typed:
  - `not_connected`, `consent_required`, `scope_missing`, `token_expired`, `access_revoked`
- drive provider failures are typed and user-recoverable:
  - `provider_timeout`, `provider_network_failure`, `provider_rate_limited`,
    `provider_upstream_failure`, `provider_permission_denied`, `provider_request_rejected`,
    `resource_unavailable`, `provider_invalid_payload`, `provider_unreachable`
- drive read typed outcomes are explicit and bounded:
  - `unsupported` (`drive_read_unsupported`)
  - `too_large` (`drive_read_too_large`)
  - `unavailable` (`drive_read_unavailable`)
- drive read/search outputs stay retrieval-style with inline citations and `assistant.sources[]`,
  preserving grounded answer synthesis behavior.

## slice-6 pr-02 maps read vertical (directions + nearby places)

slice-6 pr-02 adds maps retrieval capabilities under explicit read-only policy and fail-closed egress:

- allowlisted read capabilities (no approval path):
  - `cap.maps.directions`
  - `cap.maps.search_places`
- maps execution uses server-managed provider credentials only (no google oauth reconnect/consent loop).
- maps capability contracts remain strict and retrieval-native (citation-ready `results[]` + `retrieved_at`).
- required-field clarification behavior is deterministic and explicit:
  - `maps_origin_required`
  - `maps_destination_required`
  - `maps_location_context_required`
- maps credential/config failures are typed and recoverable:
  - `provider_credentials_missing`
  - `provider_credentials_invalid`
- maps provider/runtime failures are typed and recoverable:
  - `provider_timeout`, `provider_network_failure`, `provider_rate_limited`,
    `provider_upstream_failure`, `provider_permission_denied`, `provider_request_rejected`,
    `provider_invalid_payload`, `provider_unreachable`
- maps retrieval remains isolated from google connector readiness/consent state.
- maps outputs stay grounded with inline citations and `assistant.sources[]` in single- and mixed-retrieval turns.

## slice-7 pr-01 url extraction vertical (cap.web.extract)

slice-7 pr-01 adds url extraction retrieval under strict safety preflight, fail-closed egress, and
grounded provenance contracts:

- allowlisted read capability:
  - `cap.web.extract` (`read`, `allow_inline`)
- url safety preflight is strict and fail-closed before extraction:
  - invalid url -> `url_invalid`
  - non-http(s) scheme -> `url_scheme_unsupported`
  - unsafe destination posture -> `url_destination_unsafe`
- extraction egress remains explicit and fail-closed:
  - capabilities must declare outbound intent
  - undeclared/malformed/non-allowlisted destinations are blocked before outbound dispatch
- successful extraction returns bounded structured content with grounded citation surfacing:
  - inline citation markers in `assistant.message`
  - synchronized `assistant.sources[]`
  - inspectable provenance via `GET /v1/artifacts/{artifact_id}`
- canonical source identity remains stable across retries/redirect-normalization for dedupe-safe citation
  and provenance linkage.
- typed extraction/runtime failure outcomes remain explicit and actionable:
  - `access_restricted`, `unsupported_format`
  - `provider_timeout`, `provider_network_failure`, `provider_rate_limited`,
    `provider_upstream_failure`, `provider_request_rejected`, `provider_invalid_payload`,
    `provider_unreachable`
- large/complex pages remain bounded with explicit partial disclosure (no silent degradation).
- mixed turns containing `cap.web.extract` plus non-retrieval proposals keep retrieval-grounded
  assistant messaging while preserving structured lifecycle inspectability for all proposals.

## slice-8 quick capture surface (`post /v1/captures`)

slice-8 adds first-class quick capture ingress for bounded text, url, and shared-content payloads:

- request shape:
  - `kind="text"` requires `text`
  - `kind="url"` requires `url`
  - `kind="shared_content"` requires `shared_content` with optional `text` and `urls[]`
  - optional `note`
  - optional `source` object (`app`, `title`, `url`)
- client does not provide a session id; ariel resolves effective active session server-side and
  runs the same turn/orchestration/action lifecycle used by chat turns.
- responses are strict surfaced contracts:
  - success: `{ok, capture, session, turn, assistant}`
  - failure: `{ok, capture, error}`
- idempotency is request-scoped and optional via `Idempotency-Key`:
  - same key + same payload replays prior capture/turn outcome
  - same key + different payload returns `409 E_IDEMPOTENCY_KEY_REUSED`
- capture ingress failures are durable and typed (`E_CAPTURE_*`) and are explicitly separated from
  in-turn failures (`capture.terminal_state="turn_created"` with typed `error`).

google connector runtime config:

- `ARIEL_GOOGLE_OAUTH_CLIENT_ID`
- `ARIEL_GOOGLE_OAUTH_CLIENT_SECRET`
- `ARIEL_GOOGLE_OAUTH_REDIRECT_URI` (default `http://127.0.0.1:8000/v1/connectors/google/callback`)
- `ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS` (default `600`)
- `ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS` (default `10.0`)
- `ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION` (default `v1`)
- `ARIEL_CONNECTOR_ENCRYPTION_KEYS` (recommended for production key rotation)
- `ARIEL_CONNECTOR_ENCRYPTION_SECRET` (fallback/dev secret path only)

`ARIEL_CONNECTOR_ENCRYPTION_KEYS` accepts either:

- JSON object: `{"v1":"<base64url-key>","v2":"<base64url-key>"}`
- comma list: `v1:<base64url-key>,v2:<base64url-key>`

use 16/24/32-byte keys (base64url encoded). keep previous key versions configured during rotation windows.

search capability runtime config:

- `ARIEL_SEARCH_WEB_API_KEY` (required for live web retrieval backend)
- `ARIEL_SEARCH_WEB_ENDPOINT` (optional; defaults to Brave web search endpoint)
- `ARIEL_SEARCH_WEB_TIMEOUT_SECONDS` (optional; defaults to `8.0`)
- `ARIEL_SEARCH_NEWS_API_KEY` (optional; falls back to `ARIEL_SEARCH_WEB_API_KEY`)
- `ARIEL_SEARCH_NEWS_ENDPOINT` (optional; defaults to Brave news search endpoint)
- `ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS` (optional; defaults to `8.0`)

weather capability runtime config:

- `ARIEL_WEATHER_PROVIDER_MODE` (`production` default, `dev_fallback` optional)
- `ARIEL_WEATHER_PRODUCTION_ENDPOINT` (optional; defaults to Tomorrow.io forecast endpoint)
- `ARIEL_WEATHER_PRODUCTION_API_KEY` (required for production weather backend)
- `ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS` (optional; defaults to `8.0`)
- `ARIEL_WEATHER_DEV_ENDPOINT` (optional; defaults to `https://wttr.in`)
- `ARIEL_WEATHER_DEV_TIMEOUT_SECONDS` (optional; defaults to `8.0`)
- `ARIEL_WEATHER_DEFAULT_LOCATION` (optional bootstrap-only fallback; seeded once when canonical state is unset)

maps capability runtime config:

- `ARIEL_MAPS_PROVIDER_API_KEY_ENC` (required; encrypted maps provider api key)
- `ARIEL_MAPS_PROVIDER_ENDPOINT` (optional; defaults to `https://maps.googleapis.com/maps/api`)
- `ARIEL_MAPS_PROVIDER_TIMEOUT_SECONDS` (optional; defaults to `8.0`)

maps encrypted key handling uses the existing connector cipher/keyring settings:

- `ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION`
- `ARIEL_CONNECTOR_ENCRYPTION_KEYS`
- `ARIEL_CONNECTOR_ENCRYPTION_SECRET`

web extract capability runtime config:

- `ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT` (optional; defaults to Brave extract endpoint)
- `ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS` (optional; defaults to `10.0`)
- `ARIEL_WEB_EXTRACT_MAX_RETRIES` (optional; defaults to `2`, max `5`)
- `ARIEL_WEB_EXTRACT_API_KEY` (optional; falls back to `ARIEL_SEARCH_WEB_API_KEY`)

weather default location APIs:

```bash
GET /v1/weather/default-location
PUT /v1/weather/default-location
```

approval decisions are handled through:

```bash
POST /v1/approvals
```

request body:

```json
{
  "approval_ref": "apr_xxx",
  "decision": "approve",
  "actor_id": "user.local",
  "reason": "optional"
}
```

response body:

```json
{
  "ok": true,
  "approval": {
    "reference": "apr_xxx",
    "status": "approved|denied|expired",
    "reason": null,
    "expires_at": "2026-03-03T07:00:00Z",
    "decided_at": "2026-03-03T06:59:30Z"
  },
  "assistant": {
    "message": "approved action executed successfully."
  }
}
```

the endpoint is single-use, actor-bound, expiry-bound, executes only the frozen proposed payload,
and exposes surfaced approval state only (no internal action-attempt object in response).

slice-2 pr-02/pr-03 hardening adds runtime boundary checks for side effects:

- runtime-provenance taint authorization for side-effecting proposals; model taint flags are advisory-only and cannot clear runtime taint.
- fail-closed taint handling for side effects: tainted/ambiguous `write_reversible` requires approval; tainted/ambiguous `write_irreversible` and `external_send` are denied.
- action lifecycle events include taint provenance evidence and decision-basis metadata for audit reconstruction.
- proposal-time execution identity capture (`capability_id`, `capability_version`, `capability_contract_hash`) and execution-time integrity enforcement.
- deny-by-default outbound control via preflight-declared per-capability destination allowlists; non-allowlisted egress is blocked before dispatch.
- pre-execution input guardrails and post-execution output guardrails before side effects/user surfacing.
- serialized side-effect execution gates during approval-triggered runs using transactional postgres advisory locks.

see `docs/v1/s2/s2_prs/s2_pr02_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr03_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr04_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr05_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr06_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr07_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr08_implementation_notes.md` and
`docs/v1/s3/s3_prs/s3_pr01_implementation_notes.md` and
`docs/v1/s3/s3_prs/s3_pr02_implementation_notes.md` and
`docs/v1/s3/s3_prs/s3_pr03_implementation_notes.md` and
`docs/v1/s4/s4_prs/s4_pr01_implementation_notes.md` and
`docs/v1/s4/s4_prs/s4_pr02_implementation_notes.md` and
`docs/v1/s4/s4_prs/s4_pr03_implementation_notes.md` and
`docs/v1/s6/s6_prs/s6_pr01_implementation_notes.md` and
`docs/v1/s6/s6_prs/s6_pr02_implementation_notes.md` and
`docs/v1/s7/s7_prs/s7_pr01_implementation_notes.md` and
`docs/v1/s8/s8_prs/s8_pr01_implementation_notes.md` for implementation details and tradeoffs.

## run locally

create local config once (no repeated `export`/`source` needed). app + alembic auto-load `.env.local`.

```bash
make env-init
# edit .env.local with real values for your machine
```

inspect resolved local-db runtime config (derived from `ARIEL_DATABASE_URL`):

```bash
make db-config
```

manage local postgres with docker (idempotent):

```bash
make db-up
make db-status
make db-logs
# when needed:
make db-stop
make db-down
make db-destroy
```

start app in real provider mode:

```bash
make db-upgrade
make run
```

or run end-to-end with one command:

```bash
make dev
```

switch model provider cleanly:

```bash
make run-openai
make run-echo
```

connection-string values (`user/password/database/port`) can be any values you want, as long as:

- they are valid PostgreSQL credentials,
- they match the database actually running,
- and for `make db-up` they point to loopback host (`localhost`, `127.0.0.1`, or `::1`).

explicit shell env vars still override `.env.local` when set.

slice-1 turn budgets are runtime-configurable:

- `ARIEL_MAX_RECENT_TURNS` (default `12`) bounds how many prior turns are included in the deterministic turn context bundle.
- `ARIEL_MAX_CONTEXT_TOKENS` (default `6000`) bounds estimated prompt/context tokens for a turn.
- `ARIEL_AUTO_ROTATE_MAX_TURNS` (default `120`) rotates on turn boundary when prior turn count meets/exceeds threshold.
- `ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS` (default `172800`) rotates on turn boundary when session age meets/exceeds threshold.
- `ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS` (default `5400`) rotates on turn boundary when estimated context pressure meets/exceeds threshold.
- `ARIEL_MAX_RESPONSE_TOKENS` (default `700`) bounds assistant completion tokens per turn.
- `ARIEL_MAX_MODEL_ATTEMPTS` (default `2`) bounds retryable model attempts per turn.
- `ARIEL_MAX_TURN_WALL_TIME_MS` (default `20000`) bounds total turn processing wall time.
- `ARIEL_APPROVAL_TTL_SECONDS` (default `900`) sets approval expiry window for approval-gated actions.
- `ARIEL_APPROVAL_ACTOR_ID` (default `user.local`) sets the expected actor for approval-bound actions.

when a configured turn budget is exhausted, `POST /v1/sessions/{session_id}/message` returns HTTP `429` with
`E_TURN_LIMIT_REACHED` and structured limit details (`budget`, `unit`, `limit`, `measured`, `applied_limits`).
turn event chains remain auditable and include explicit bounded-failure emission before terminal `evt.turn.failed`.

if migrations are missing, `/v1/*` endpoints return `E_SCHEMA_NOT_READY` (503) until schema is upgraded.

smoke-check the key surfaces:

```bash
curl -sS http://127.0.0.1:8000/v1/health
curl -sS http://127.0.0.1:8000/v1/sessions/active
curl -sS -X POST http://127.0.0.1:8000/v1/captures \
  -H "content-type: application/json" \
  -H "Idempotency-Key: smoke-capture-001" \
  -d '{"kind":"text","text":"smoke capture"}'
curl -sS http://127.0.0.1:8000/
```

## private tailnet deployment

for the pr-02 private ingress setup + restart durability verification workflow, use:

- `docs/v1/s0/private_tailnet_runbook.md`
