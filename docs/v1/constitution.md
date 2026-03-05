# ariel — constitution v3

## 1. vision

### problem
Work and life actions are fragmented across tools and devices. Away from a desk, it is hard to execute high-leverage actions reliably.

### solution
Ariel is a private, self-hosted assistant that accepts natural language and multimodal input, uses model-led open-ended reasoning inside bounded runtime guardrails, and executes actions through typed capabilities with policy checks, approvals, and full auditability.

### scope (mvp)
- Phone-first surface (web chat plus speech-to-text/text-to-speech push-to-talk voice I/O) over Tailscale.
- Core orchestrator loop: intake -> context build -> think/plan -> tool calls -> response.
- Capability system with strict input/output schemas and policy enforcement.
- Provider-agnostic external knowledge retrieval (web search/news/weather and URL extraction) with user-visible provenance.
- Google Workspace integration (calendar, email, drive, maps) with approval-gated side effects.
- `agency` integration is later-phase roadmap work (`Slice 10`) after additional MCP hardening.
- Nexus notes integration is deferred indefinitely (unscheduled) and is not a current MVP dependency.
- Vision-capable image understanding in conversation and capture workflows.
- Proactive notification layer with user-configured scheduled checks and notification delivery.
- Quick-capture entry points (share sheet, clipboard, shortcuts) that ingest into the active session.
- Append-only event log plus structured logs with redaction.
- Provider-agnostic model router (swap providers without core refactors).
- Episodic conversation sessions with durable cross-session memory.

### non-scope (mvp)
- Plugin marketplace, third-party skill auto-install, or arbitrary dynamic code loading.
- Generic shell/ssh capability exposed to the model.
- Autonomous open-ended background agents; only bounded user-configured proactive checks/notifications are allowed.
- Fully automated external sending (email/post) outside subscription-bound notification policy; draft or approval-gated only.
- Phone-call placement/receiving (voice telephony).
- Financial transaction or payment automation.
- Smart-home device control.
- Real-time always-on streaming voice conversation.
- Multi-user tenancy or public internet hosting.
- Unbounded "forever context replay" per turn.

---

## 2. core abstractions

| concept | definition |
|---|---|
| **surface** | Authenticated channel where the user interacts (web now, voice/mobile later). |
| **session** | One episodic conversation with short-term continuity. Exactly one active session per user at a time. |
| **turn** | One user input and the resulting sequence of model/tool events. |
| **capability** | Typed tool contract executed under schema, policy, timeout, and output guardrails. |
| **action attempt** | Durable record for one proposed capability call and its lifecycle (`proposed -> awaiting_approval -> approved|denied|expired -> executing -> succeeded|failed`). |
| **memory record** | Durable cross-session fact/preference/commitment with provenance and confidence. |
| **event log** | Append-only system-of-record stream for turns, model calls, tools, approvals, and jobs. |

---

## 3. architecture

### components

```text
+------------------------------------------+      HTTPS (tailnet only)      +-----------------------------+
| surface (web, voice, capture ingress)    | -----------------------------> | core API + orchestrator     |
| one active session                        |                                 | session/memory builder       |
+------------------------------------------+                                 | policy + approvals           |
                                                                             | event log writer             |
                                                                             +-----------+-----------------+
                                                                                         | typed internal RPC
                                                                                         v
                                                                             +-----------------------------+
                                                                             | executor (cap runtime)      |
                                                                             | schema validation            |
                                                                             | timeout/output limits        |
                                                                             | adapters: google/brave/      |
                                                                             | web-extract (+agency later)  |
                                                                             +-----------+-----------------+
                                                                                         |
                                                                                         v
                                                                             +-----------------------------+
                                                                             | local/remote services       |
                                                                             | google apis, brave,         |
                                                                             | secret mgr (+agency later)  |
                                                                             +-----------------------------+
                                                                                         ^
                                                                                         |
                                                                             +-----------------------------+
                                                                             | notification scheduler      |
                                                                             | (subscription-bound,        |
                                                                             | read-only checks)           |
                                                                             +-----------------------------+
```

### trust model
- Surfaces are authenticated clients and never execute tools directly.
- Core is the only component allowed to call model providers and authorize tool execution.
- Executor is least privilege and only returns schema-valid structured outputs.
- Model outputs are untrusted until schema + policy checks pass.
- External/untrusted content is data only and cannot mutate policy, prompts, or permissions.
- Untrusted content cannot independently authorize side effects; policy must explicitly allow, escalate to approval, or deny.
- Proactive scheduler flows are policy-bounded and cannot authorize side effects.

---

## 4. hard constraints

| constraint | value |
|---|---|
| language/runtime | Python only (`fastapi` + async workers). |
| deployment | Single-user self-hosted home machine, reachable over private Tailscale network. |
| remote exposure | No public ingress in MVP; service binds localhost and is proxied via Tailscale Serve. |
| storage | Postgres is the system of truth for sessions, turns, memories, jobs, notifications, and events. |
| memory authority | External stores (including Nexus when enabled) are projections/integrations; they are never canonical memory SoT. |
| conversation model | Episodic sessions, not one unbounded forever thread. |
| context model | Deterministic bounded context builder per turn. |
| response policy | No hard-coded response-type state machine; the model decides assistant messaging while runtime guardrails enforce safety and limits. |
| model providers | Pluggable adapters behind one internal interface. |
| retrieval provider portability | External web/news retrieval runs through Ariel capability contracts independent of model-provider built-in search tools. |
| factual grounding policy | External factual claims from web/news/weather are citation-gated: user-visible references are required, and insufficient/conflicting evidence must be disclosed as uncertainty. |
| weather location policy | Weather location resolution order is explicit location first, configured default second, clarification otherwise; implicit IP/device geolocation inference is not used in MVP. |
| tool execution | No generic shell/ssh capability in MVP; when code-change capabilities are enabled, they route through `cap.agency.*` only. |
| approvals | Required for irreversible or externally visible actions. |
| side-effect execution model | Side-effecting capability calls are serialized for deterministic safety/audit behavior in MVP. |
| oauth connector model | External connectors use OAuth authorization-code + PKCE with short-lived state and strict callback validation; token material is encrypted at rest. |
| proactive execution model | Proactive checks are read-only and subscription-bound; notification delivery does not require per-notification approval. |
| quality gate model | Capability and orchestration changes must pass regression evaluations for grounding, safety, reliability, and multimodal behavior before release. |
| egress model | Capability execution uses explicit destination allowlists; arbitrary outbound network access is denied by default. |
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
- notification subscription ids: `sub_<ulid>`
- capture ids: `cpt_<ulid>`

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
  - `read`: no approval only for explicitly allowlisted low-impact read capabilities.
  - `write_reversible`: approval unless explicitly allowlisted.
  - `write_irreversible` and `external_send`: always requires approval.
- Approval token must include hash of exact action payload, actor id, and expiry.
- Approval tokens are single-use and authorize execution of the frozen proposed payload (no re-planning at approval time).
- Proposals derived from untrusted external/tool content cannot auto-authorize side effects; policy escalates to approval or denies.

### proactive notifications
- Notification delivery is subscription-bound (explicit user opt-in, schedule, and channel policy) and does not require per-notification approval.
- Proactive checks can invoke `read` capabilities only.
- Proactive flows cannot execute side-effecting capabilities unless the user initiates a normal turn and approval policy passes.

### capability integrity and egress
- Execution is bound to stable capability identity/version and contract metadata captured at proposal time.
- Any capability identity/contract mismatch at execution time is blocked and auditable.
- Capability outbound access is constrained to explicit policy-allowed destinations.

### model interface
- Internal adapter:
  - `respond(messages, tools, config) -> {assistant_text, tool_calls[], usage, provider_response_id}`
- Turn orchestration may iterate model <-> tool execution until terminal assistant output or bounded failure, with global limits on attempts/tokens/wall time.
- Invalid JSON or schema-invalid tool call is treated as planning failure, never executed.

### context builder (deterministic order)
1. System/policy instructions.
2. Current session recent turns (bounded tail).
3. Rolling summary of current session.
4. Retrieved durable memories (top-k, scored by relevance/recency/confidence).
5. Open commitments/jobs relevant to this turn.
6. Relevant source artifacts/proactive signals for the current request context (bounded and auditable).

---

## 6. invariants

1. No capability executes without schema validation and policy authorization.
2. Any approval-required action must match the approved payload hash exactly.
3. Every turn writes a complete event chain: `turn.started`, model/tool events, and terminal `turn.completed|turn.failed`.
4. Secrets are injected only at executor runtime; they are never included in model prompts.
5. No generic remote code execution capability exists in MVP.
6. When repo/code modification capabilities are enabled, all such actions initiated by Ariel must route via `cap.agency.*`.
7. Untrusted external content cannot mutate policy, capability permissions, or system prompts.
8. Each user has at most one active session at a time; sessions rotate by policy without losing durable memory.
9. Context assembly is deterministic, bounded, and auditable for every turn.
10. Every capability call has timeout and max output constraints; truncation is explicit.
11. Jobs use durable state transitions with idempotency for side-effecting operations.
12. User can inspect executed capability calls, approvals, job status, and artifacts in the surface.
13. Untrusted content cannot independently authorize side-effecting capability execution.
14. Approval decisions are single-use and execute only the exact approved payload.
15. Side-effecting capability execution is serialized in MVP.
16. Capability identity/contract mismatch at execution time blocks execution.
17. Capability outbound access is limited to policy-allowed destinations.
18. Postgres is canonical memory SoT; external systems (including Nexus when enabled) are projections and cannot silently overwrite canonical memory.
19. Proactive scheduler executions are read-only and cannot trigger side-effecting capabilities.
20. Every proactive notification is linked to its originating subscription/check and is user-inspectable.
21. Connector credentials/scopes are least-privilege and auditable per capability.
22. External factual claims sourced from web/news/search responses include user-inspectable provenance artifacts.
23. Ariel does not present externally grounded factual claims as true without user-visible citations to provenance artifacts.
24. Weather answers resolve location deterministically (`explicit -> configured default -> clarification`) and do not rely on implicit IP/device geolocation.
25. Releases are blocked when regression evaluations fail on grounding, policy safety, reliability, or multimodal interaction quality.
26. OAuth connector state handles are single-use, short-lived, and replay-safe.
27. Connector token material is never surfaced in user APIs or logs and remains encrypted at rest.
28. Capability calls requiring connector scopes fail with typed recoverable auth/scope outcomes instead of silent fallback behavior.

---

## 7. systems of truth

### conversation state
- Event log is canonical SoT.
- Session and turn tables are materialized query views over events.
- Provider conversation ids are cache/optimization only and can be dropped without semantic loss.

### action attempts
- Action attempt lifecycle: `proposed -> awaiting_approval -> approved|denied|expired -> executing -> succeeded|failed`.
- Action-attempt events are canonical SoT for proposal, policy decision, approval decision, execution, and returned outcome.
- Turn status and action-attempt status are related but separate state machines.

### jobs
- Job lifecycle: `queued -> running -> waiting_approval -> running -> succeeded|failed|cancelled|timed_out`.
- `job_events` is canonical SoT; `jobs` table is latest snapshot.
- Side-effecting job steps require idempotency keys.

### memory
- Memory classes: `profile`, `preference`, `project`, `commitment`, `episodic_summary`.
- Each memory stores provenance (`source_turn_id`), confidence, and `last_verified_at`.
- Durable memory writes are policy-gated and auditable.
- When Nexus integration is enabled, nexus note linkage is projection metadata (reference ids + sync state), not canonical memory state.

### notifications
- Subscription configuration and notification events are canonical in Postgres.
- Delivery history is append-only and auditable.
- Notification payloads reference source artifacts/check evidence.

---

## 8. api overview (mvp, methods + paths only)

### surface API
- `POST /v1/sessions`
- `GET /v1/sessions/active`
- `POST /v1/sessions/{session_id}/message`
- `GET /v1/sessions/{session_id}/events?after={event_id}`
- `POST /v1/connectors/google/start`
- `GET /v1/connectors/google/callback`
- `GET /v1/connectors/google`
- `POST /v1/connectors/google/reconnect`
- `DELETE /v1/connectors/google`
- `POST /v1/captures`
- `POST /v1/approvals`
- `POST /v1/notifications/subscriptions`
- `GET /v1/notifications`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/events`
- `GET /v1/artifacts/{artifact_id}`

### executor API (internal)
- `POST /v1/exec`
- `GET /v1/health`

---

## appendix: capability set (implemented + planned/deferred)

### agency (planned later-phase, roadmap slice 10)
- `cap.agency.run`
- `cap.agency.status`
- `cap.agency.artifacts`
- `cap.agency.request_pr` (approval required if it pushes remote changes)

### search
- `cap.search.web` (`read`, provider-agnostic; Brave-backed by default in MVP)
- `cap.search.news` (`read`)

### weather
- `cap.weather.forecast` (`read`)

### web
- `cap.web.extract` (`read`)

### email
- `cap.email.search` (`read`)
- `cap.email.read` (`read`)
- `cap.email.draft` (`write_reversible`)
- `cap.email.send` (`external_send`, approval required)

### calendar
- `cap.calendar.list` (`read`)
- `cap.calendar.propose_slots` (`read`)
- `cap.calendar.create_event` (`write_reversible`, approval required)

### drive
- `cap.drive.search` (`read`)
- `cap.drive.read` (`read`)
- `cap.drive.upload` (`write_reversible`)
- `cap.drive.share` (`external_send`, approval required)

### maps
- `cap.maps.directions` (`read`)
- `cap.maps.search_places` (`read`)

### nexus (deferred indefinitely, unscheduled)
- `cap.nexus.search` (`read`)
- `cap.nexus.read` (`read`)
- `cap.nexus.create` (`write_reversible`)
- `cap.nexus.append` (`write_reversible`)
