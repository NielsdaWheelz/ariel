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

- `POST /v1/sessions/{session_id}/message`, `GET /v1/sessions/{session_id}/events`, and
  `POST /v1/approvals` are schema-enforced surfaced contracts.
- message responses expose `assistant.message` only (not `assistant.provider/model`).
- turn events use strict per-`event_type` payload schemas (no open `events[].payload` dictionaries).
- contract drift is fail-closed with `E_RESPONSE_CONTRACT` and sanitized error details.

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
- deny-by-default outbound control via per-capability destination allowlists; non-allowlisted egress is blocked.
- pre-execution input guardrails and post-execution output guardrails before side effects/user surfacing.
- serialized side-effect execution gates during approval-triggered runs using transactional postgres advisory locks.

see `docs/v1/s2/s2_prs/s2_pr02_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr03_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr04_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr05_implementation_notes.md` and
`docs/v1/s2/s2_prs/s2_pr06_implementation_notes.md` for implementation details and tradeoffs.

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
curl -sS http://127.0.0.1:8000/
```

## private tailnet deployment

for the pr-02 private ingress setup + restart durability verification workflow, use:

- `docs/v1/s0/private_tailnet_runbook.md`
