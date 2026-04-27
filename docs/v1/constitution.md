# ariel — constitution v4

## 1. vision

### problem
Personal work actions are fragmented across tools. Ariel should let the owner ask once, then track and execute bounded work reliably.

### solution
Ariel is a private Discord-primary assistant backed by a FastAPI core, durable Postgres state, and explicit background workers. It accepts natural language, plans with model help, executes typed capabilities under policy, and records every turn, job, event, notification, and approval.

### scope
- Discord is the primary user surface for chat, approvals, job status, and notifications.
- FastAPI remains the internal HTTP core for orchestration, surface adapters, health, and typed ingress.
- Postgres is canonical for sessions, turns, memories, jobs, job events, agency events, notifications, and audit events.
- A worker process owns durable background job execution, scheduled checks, retries, and notification delivery.
- Agency integration uses signed HTTP ingress/events and durable job records.
- Google Workspace, web/news/weather, maps, drive, URL extraction, and memory remain capability-driven.
- External factual claims from web/news/weather/search require user-visible provenance.

### non-scope
- No web/PWA/phone frontend as the primary surface.
- No legacy frontend compatibility requirement for the phone web surface.
- No inbound MCP control plane for Ariel.
- No plugin marketplace, arbitrary dynamic code loading, or generic model-exposed shell/ssh.
- No autonomous unbounded background agents; background work is job/subscription bounded.
- No public multi-user tenancy.
- No always-on streaming voice.
- No smart-home, payment, or financial transaction automation.

---

## 2. core abstractions

| concept | definition |
|---|---|
| **surface** | Authenticated adapter where the owner interacts. Discord is primary. |
| **session** | One episodic conversation with short-term continuity. |
| **turn** | One user input and the resulting model/tool/event sequence. |
| **capability** | Typed tool contract executed under schema, policy, timeout, and output limits. |
| **action attempt** | Durable lifecycle for one proposed capability call. |
| **job** | Durable background unit of work with retry, timeout, cancellation, and event history. |
| **agency event** | Signed external event from Agency linked to a job/action. |
| **notification** | Durable user-visible message emitted from jobs, scheduled checks, or system events. |
| **memory record** | Durable cross-session fact/preference/commitment with provenance and confidence. |
| **event log** | Append-only system-of-record stream for turns, tools, approvals, jobs, agency, and notifications. |

---

## 3. architecture

```text
Discord
  |
  | bot worker / surface adapter
  v
+----------------------------+
| FastAPI core               |
| sessions + orchestration   |
| policy + approvals         |
| signed HTTP ingress        |
| health/admin APIs          |
+-------------+--------------+
              |
              v
+----------------------------+
| Postgres                   |
| sessions, turns, memories  |
| jobs, job_events           |
| agency_events              |
| notifications, audit       |
+-------------+--------------+
              ^
              |
+-------------+--------------+
| worker process             |
| job runner, scheduler      |
| retries, notifications     |
| agency event processing    |
+-------------+--------------+
              |
              v
+----------------------------+
| capability/runtime adapters|
| google, search, weather    |
| maps, drive, web, agency   |
+----------------------------+
```

### trust model
- Discord authenticates the owner-facing surface; Ariel still authorizes every action server-side.
- Surface adapters never execute capabilities directly.
- FastAPI core validates requests, verifies signed ingress, authorizes actions, and writes canonical events.
- Worker processes claim durable Postgres jobs and are idempotent.
- Model output is untrusted until schema and policy checks pass.
- External/untrusted content is data only and cannot mutate policy, prompts, scopes, or approvals.
- Agency ingress is signed HTTP only; unsigned or replayed events are rejected.

---

## 4. hard constraints

| constraint | value |
|---|---|
| language/runtime | Python FastAPI core plus Python workers. |
| primary surface | Discord. |
| frontend posture | No web/PWA/phone frontend primary path or compatibility promise. |
| deployment | Single-user self-hosted service. |
| storage | Postgres is canonical for runtime and audit state. |
| background execution | Durable Postgres jobs/events, claimed by worker processes. |
| agency ingress | Signed HTTP callbacks/events, persisted before processing. |
| remote control | No inbound MCP control plane. |
| future daemons | Optional standalone Rust daemons only when a narrow runtime need justifies them. |
| conversation model | Episodic sessions, not one unbounded forever thread. |
| context model | Deterministic bounded context builder per turn/job. |
| model providers | Pluggable adapters behind one internal interface. |
| retrieval portability | Web/news/weather runs through Ariel capabilities, not provider-locked search tools. |
| factual grounding | External factual claims need citations/provenance or explicit uncertainty. |
| tool execution | No generic shell/ssh capability; code work routes through `cap.agency.*`. |
| approvals | Required for irreversible or externally visible actions. |
| side effects | Serialized where needed for deterministic safety and auditability. |
| connector secrets | Encrypted at rest; never surfaced in prompts, logs, Discord, or API responses. |
| egress | Explicit destination allowlists; deny by default. |
| observability | JSON logs plus append-only events are mandatory. |

---

## 5. conventions

### names
- capability ids: `cap.<domain>.<verb>`
- event types: `evt.<entity>.<action>`
- session ids: `ses_<ulid>`
- turn ids: `trn_<ulid>`
- job ids: `job_<ulid>`
- agency event ids: `age_<ulid>`
- memory ids: `mem_<ulid>`
- notification ids: `ntf_<ulid>`

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

### capability contract

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
- JSON logs only.
- Required fields: `ts`, `level`, `component`, `event_type`, `session_id?`, `turn_id?`, `job_id?`, `capability?`.
- Redact keys matching `token|secret|key|authorization|cookie`.

### policy
- Impact levels: `read | write_reversible | write_irreversible | external_send`.
- `read`: allowed only for explicitly allowlisted low-impact capabilities.
- `write_reversible`: approval unless explicitly allowlisted.
- `write_irreversible` and `external_send`: always approval-gated.
- Approval tokens bind actor, expiry, exact payload hash, capability identity, and contract hash.
- Approval executes the frozen proposed payload only.

### jobs
- Lifecycle: `queued -> running -> waiting_approval -> running -> succeeded|failed|cancelled|timed_out`.
- Workers claim jobs transactionally and heartbeat while running.
- Side-effecting steps require idempotency keys.
- Every job emits append-only `job_events`.
- Job status is surfaced through Discord notifications and explicit status responses.

### agency
- Agency work is represented as Ariel jobs.
- Agency callbacks use signed HTTP ingress with replay protection.
- Agency event payloads are persisted before downstream processing.
- External or irreversible Agency outcomes remain approval-gated before Ariel requests them.

### notifications
- Notifications are durable Postgres records.
- Discord is the primary delivery channel.
- Delivery attempts are append-only with dedupe/idempotency keys and retry/backoff state.
- Scheduled/proactive checks can invoke read capabilities only.

---

## 6. invariants

1. No capability executes without schema validation and policy authorization.
2. Approval-required actions execute only the exact approved payload.
3. Every turn writes a complete append-only event chain.
4. Every background job writes durable state transitions and events.
5. Agency ingress is signed, replay-safe, and persisted before processing.
6. Secrets never enter model prompts, logs, Discord messages, or user API responses.
7. Discord is the primary surface; web/PWA phone compatibility is not protected.
8. No inbound MCP control plane exists.
9. No generic model-exposed shell/ssh exists.
10. Code work initiated by Ariel routes through `cap.agency.*`.
11. Context assembly is deterministic, bounded, and auditable.
12. Postgres is canonical memory/runtime/audit state.
13. External stores are projections, not canonical memory.
14. Proactive work is subscription/job bounded and cannot authorize side effects by itself.
15. External factual claims require citations/provenance or disclosed uncertainty.
16. Capability identity or contract mismatch blocks execution.
17. Capability outbound access is allowlisted.
18. Connector credentials are least-privilege, encrypted, and auditable.
19. Notification delivery is deduped, retryable, and inspectable.
20. Optional Rust daemons remain standalone helpers, not a replacement for FastAPI core.

---

## 7. systems of truth

### conversations
- Event log is canonical.
- Session/turn tables are query snapshots over events.
- Provider conversation ids are disposable cache.

### jobs
- `job_events` is canonical.
- `jobs` is latest state for claiming and querying.
- Workers are restart-safe and idempotent.

### agency
- `agency_events` is canonical for signed inbound Agency events.
- Agency events link to jobs, action attempts, artifacts, and notifications.

### memory
- Memory classes: `profile`, `preference`, `project`, `commitment`, `episodic_summary`.
- Each memory stores provenance, confidence, and verification time.
- Durable memory writes are policy-gated and auditable.

### notifications
- Notification records and delivery attempts are canonical in Postgres.
- Discord delivery state is a projection.

---

## 8. API overview

### surface/core API
- `POST /v1/sessions`
- `GET /v1/sessions/active`
- `POST /v1/sessions/{session_id}/message`
- `GET /v1/sessions/{session_id}/events?after={event_id}`
- `POST /v1/approvals`
- `POST /v1/captures`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/events`
- `GET /v1/notifications`
- `POST /v1/notifications/{notification_id}/ack`
- `GET /v1/artifacts/{artifact_id}`

### connector API
- `POST /v1/connectors/google/start`
- `GET /v1/connectors/google/callback`
- `GET /v1/connectors/google`
- `POST /v1/connectors/google/reconnect`
- `DELETE /v1/connectors/google`

### signed ingress
- `POST /v1/agency/events`

### internal health
- `GET /v1/health`

---

## 9. capability set

### agency
- `cap.agency.run`
- `cap.agency.status`
- `cap.agency.artifacts`
- `cap.agency.request_pr` (approval required when it pushes or changes remote state)

### search/weather/web
- `cap.search.web`
- `cap.search.news`
- `cap.weather.forecast`
- `cap.web.extract`

### google workspace
- `cap.calendar.list`
- `cap.calendar.propose_slots`
- `cap.calendar.create_event`
- `cap.email.search`
- `cap.email.read`
- `cap.email.draft`
- `cap.email.send`
- `cap.drive.search`
- `cap.drive.read`
- `cap.drive.upload`
- `cap.drive.share`

### maps
- `cap.maps.directions`
- `cap.maps.search_places`
