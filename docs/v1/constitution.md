# ariel — constitution v2

## 1. vision

### problem
Work and life actions are fragmented across tools and devices. Away from a desk, it is hard to execute high-leverage actions reliably.

### solution
Ariel is a private, self-hosted assistant that accepts natural language, plans with bounded reasoning, and executes actions through typed capabilities with policy checks, approvals, and full auditability.

### scope (mvp)
- Phone-first chat UI (web) over Tailscale.
- Core orchestrator loop: intake -> context build -> think/plan -> tool calls -> response.
- Capability system with strict input/output schemas and policy enforcement.
- First-class `agency` integration (start runs, check status, fetch artifacts).
- Calendar read/propose; event creation requires approval.
- Append-only event log plus structured logs with redaction.
- Provider-agnostic model router (swap providers without core refactors).
- Episodic conversation sessions with durable cross-session memory.

### non-scope (mvp)
- Plugin marketplace, third-party skill auto-install, or arbitrary dynamic code loading.
- Generic shell/ssh capability exposed to the model.
- Fully autonomous background agents without an initiating user request (except status polling for a user-started job).
- Fully automated external sending (email/message/post); draft or approval-gated only.
- Multi-user tenancy or public internet hosting.
- Unbounded "forever context replay" per turn.

---

## 2. core abstractions

| concept | definition |
|---|---|
| **surface** | Authenticated channel where the user interacts (web now, voice/mobile later). |
| **session** | One episodic conversation with short-term continuity. Exactly one active session per user at a time. |
| **turn** | One user input and the resulting sequence of model/tool events. |
| **memory record** | Durable cross-session fact/preference/commitment with provenance and confidence. |
| **context bundle** | Deterministically assembled prompt context for a turn (session tail + summary + retrieved memory + open commitments). |
| **capability** | Typed tool contract: `{name, version, input_schema, output_schema, impact, approval_policy, timeout}`. |
| **policy** | Deterministic allow/deny/confirm rules for capability execution and memory writes. |
| **approval** | User confirmation bound to exact action payload hash and expiry. |
| **job** | Async unit of work with durable status, events, and artifacts. |
| **event log** | Append-only system-of-record stream for turns, model calls, tools, approvals, and jobs. |

---

## 3. architecture

### components

```text
+----------------------+      HTTPS (tailnet only)      +-------------------------+
| surface (web mvp)    | -----------------------------> | core API + orchestrator |
| one active session   |                                 | session/memory builder   |
+----------------------+                                 | policy + approvals       |
                                                         | event log writer         |
                                                         +------------+------------+
                                                                      |
                                                                      | typed internal RPC
                                                                      v
                                                         +-------------------------+
                                                         | executor (cap runtime)  |
                                                         | schema validation        |
                                                         | timeout/output limits    |
                                                         | adapters: agency, cal    |
                                                         +------------+------------+
                                                                      |
                                                                      v
                                                         +-------------------------+
                                                         | local/remote services   |
                                                         | agency, google apis,    |
                                                         | secret manager           |
                                                         +-------------------------+
```

### trust model
- Surfaces are authenticated clients and never execute tools directly.
- Core is the only component allowed to call model providers and authorize tool execution.
- Executor is least privilege and only returns schema-valid structured outputs.
- Model outputs are untrusted until schema + policy checks pass.
- External/untrusted content is data only and cannot mutate policy, prompts, or permissions.

---

## 4. hard constraints

| constraint | value |
|---|---|
| language/runtime | Python only (`fastapi` + async workers). |
| deployment | Single-user self-hosted home machine, reachable over private Tailscale network. |
| remote exposure | No public ingress in MVP; service binds localhost and is proxied via Tailscale Serve. |
| storage | Postgres is the system of truth for sessions, turns, memories, jobs, and events. |
| conversation model | Episodic sessions, not one unbounded forever thread. |
| context model | Deterministic bounded context builder per turn. |
| model providers | Pluggable adapters behind one internal interface. |
| tool execution | No generic shell/ssh capability in MVP; code changes go through `cap.agency.*`. |
| approvals | Required for irreversible or externally visible actions. |
| observability | Structured JSON logs and append-only event log are mandatory. |
| redaction | Secrets never enter prompts or logs; UI output is scrubbed by default. |

---

## 5. conventions

### naming
- capability ids: `cap.<domain>.<verb>` (example: `cap.agency.run`)
- event types: `evt.<entity>.<action>` (example: `evt.turn.started`)
- session ids: `ses_<ulid>`
- turn ids: `trn_<ulid>`
- job ids: `job_<ulid>`
- memory ids: `mem_<ulid>`

### ids and timestamps
- IDs: ULID
- timestamps: RFC3339 UTC

### standard error envelope

```json
{
  "ok": false,
  "error": {
    "code": "E_DOMAIN_REASON",
    "message": "human-readable",
    "details": {},
    "retryable": false
  }
}
```

### tool contract (required fields)

```json
{
  "name": "cap.calendar.create_event",
  "version": "1.0.0",
  "description": "Create calendar event",
  "input_schema": {},
  "output_schema": {},
  "impact": "write_reversible",
  "approval_policy": "always",
  "timeout_seconds": 30,
  "max_output_bytes": 65536,
  "idempotency_required": true,
  "required_scopes": ["google.calendar.events"]
}
```

### logging
- JSON logs only (one event per line).
- Required fields: `ts`, `level`, `component`, `event_type`, `session_id`, `turn_id`, `job_id?`, `capability?`.
- Redact keys matching case-insensitive patterns: `token|secret|key|authorization|cookie`.

### policy and approval
- Capability impact levels: `read | write_reversible | write_irreversible | external_send`.
- Enforcement:
  - `read`: no approval.
  - `write_reversible`: approval unless explicitly allowlisted.
  - `write_irreversible` and `external_send`: always requires approval.
- Approval token must include hash of exact action payload, actor id, and expiry.

### model interface
- Internal adapter:
  - `respond(messages, tools, config) -> {assistant_text, tool_calls[], usage, provider_response_id}`
- Invalid JSON or schema-invalid tool call is treated as planning failure, never executed.

### context builder (deterministic order)
1. System/policy instructions.
2. Current session recent turns (bounded tail).
3. Rolling summary of current session.
4. Retrieved durable memories (top-k, scored by relevance/recency/confidence).
5. Open commitments/jobs relevant to this turn.

---

## 6. invariants

1. No capability executes without schema validation and policy authorization.
2. Any approval-required action must match the approved payload hash exactly.
3. Every turn writes a complete event chain: `turn.started`, model/tool events, and terminal `turn.completed|turn.failed`.
4. Secrets are injected only at executor runtime; they are never included in model prompts.
5. No generic remote code execution capability exists in MVP.
6. All repo/code modifications initiated by Ariel must route via `cap.agency.*`.
7. Untrusted external content cannot mutate policy, capability permissions, or system prompts.
8. Each user has at most one active session at a time; sessions rotate by policy without losing durable memory.
9. Context assembly is deterministic, bounded, and auditable for every turn.
10. Every capability call has timeout and max output constraints; truncation is explicit.
11. Jobs use durable state transitions with idempotency for side-effecting operations.
12. User can inspect executed capability calls, approvals, job status, and artifacts in the surface.

---

## 7. systems of truth

### conversation state
- Event log is canonical SoT.
- Session and turn tables are materialized query views over events.
- Provider conversation ids are cache/optimization only and can be dropped without semantic loss.

### jobs
- Job lifecycle: `queued -> running -> waiting_approval -> running -> succeeded|failed|cancelled|timed_out`.
- `job_events` is canonical SoT; `jobs` table is latest snapshot.
- Side-effecting job steps require idempotency keys.

### memory
- Memory classes: `profile`, `preference`, `project`, `commitment`, `episodic_summary`.
- Each memory stores provenance (`source_turn_id`), confidence, and `last_verified_at`.
- Durable memory writes are policy-gated and auditable.

---

## 8. api overview (mvp, methods + paths only)

### surface API
- `POST /v1/sessions`
- `GET /v1/sessions/active`
- `POST /v1/sessions/{session_id}/message`
- `GET /v1/sessions/{session_id}/events?after={event_id}`
- `POST /v1/approvals`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/events`
- `GET /v1/artifacts/{artifact_id}`

### executor API (internal)
- `POST /v1/exec`
- `GET /v1/health`

---

## appendix: initial capability set

### agency
- `cap.agency.run`
- `cap.agency.status`
- `cap.agency.artifacts`
- `cap.agency.request_pr` (approval required if it pushes remote changes)

### calendar
- `cap.calendar.list` (`read`)
- `cap.calendar.propose_slots` (`read`)
- `cap.calendar.create_event` (`write_reversible`, approval required)
