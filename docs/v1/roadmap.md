# Ariel — Discord Cutover Roadmap

## dependency graph

```text
Slice 0: Discord Primary Skeleton
    |
    v
Slice 1: Durable Runtime Backbone
    |
    v
Slice 2: Worker + Job Execution
    |
    v
Slice 3: Safe Action Framework
 /   |   \
v    v    v
Slice 4: Read Capabilities
Slice 5: Google Workspace Core
Slice 6: Durable Memory + Session Rotation

Slice 2 -> Slice 7: Agency Signed HTTP Integration
Slice 2 -> Slice 8: Notifications + Scheduled Checks
Slice 4 -> Slice 9: Web/URL/Weather/Maps Hardening
Slice 5 -> Slice 10: Google Workspace Expansion
Slice 8 -> Slice 11: Reliability + Production Gate

Optional later: standalone Rust daemons for narrow runtime helpers only.
Deferred: web/PWA frontend, inbound MCP control plane, Nexus notes integration.
```

## slices

### Slice 0: Discord Primary Skeleton
- **Goal**: Make Discord the primary Ariel surface.
- **Outcome**: The owner can message Ariel in Discord and receive responses from the self-hosted FastAPI core.
- **Dependencies**: None
- **Acceptance**:
  - Discord bot accepts DMs from the configured owner.
  - Configured guild/channel messages are accepted.
  - Other-server messages require mention/reply.
  - Responses are posted back to Discord without slash commands.
  - The old phone web surface is not a compatibility target.

### Slice 1: Durable Runtime Backbone
- **Goal**: Put all runtime state in Postgres.
- **Outcome**: Sessions, turns, action attempts, events, jobs, agency events, notifications, and memory are durable and auditable.
- **Dependencies**: Slice 0
- **Acceptance**:
  - Service restart does not lose conversation history.
  - Every turn writes an append-only event chain.
  - Idempotency keys prevent duplicate Discord message processing.
  - Postgres is the only canonical runtime state.

### Slice 2: Worker + Job Execution
- **Goal**: Add a durable worker process for background work.
- **Outcome**: Long-running and scheduled work runs outside request handling with retry, timeout, cancellation, and status events.
- **Dependencies**: Slice 1
- **Acceptance**:
  - Jobs can be queued, claimed, heartbeated, completed, failed, timed out, and cancelled.
  - Job events are append-only and queryable.
  - Worker restarts do not duplicate side effects.
  - Discord can show job status and completion notifications.

### Slice 3: Safe Action Framework
- **Goal**: Keep tool execution deterministic and auditable.
- **Outcome**: Ariel can propose, approve, deny, and execute capability calls under policy.
- **Dependencies**: Slice 1
- **Acceptance**:
  - Read-only allowlisted actions can run without approval.
  - Approval-required actions wait for explicit owner approval.
  - Expired or denied approvals never execute.
  - Schema-invalid, unknown, or policy-denied tool calls are blocked.
  - Side-effecting actions use idempotency and serialized execution where needed.

### Slice 4: Read Capabilities
- **Goal**: Preserve useful low-risk reads through the capability framework.
- **Outcome**: Ariel can answer factual, search, news, weather, and artifact questions with provenance.
- **Dependencies**: Slice 3
- **Acceptance**:
  - External factual claims include citations or explicit uncertainty.
  - Weather location resolution is deterministic.
  - Provider failures return typed, user-visible recovery messages.
  - Retrieval does not depend on model-provider-native search.

### Slice 5: Google Workspace Core
- **Goal**: Support high-value calendar and email flows.
- **Outcome**: Ariel can read schedule/email, draft safely, and perform sends/creates only with approval.
- **Dependencies**: Slice 3
- **Acceptance**:
  - OAuth connect/reconnect/disconnect is explicit and recoverable.
  - Calendar/email reads are allowlisted.
  - Calendar creation and email send are approval-gated.
  - Scope failures are typed and remediable.

### Slice 6: Durable Memory + Session Rotation
- **Goal**: Preserve continuity without unbounded context.
- **Outcome**: Ariel rotates sessions while retaining validated preferences, commitments, and project context.
- **Dependencies**: Slice 3
- **Acceptance**:
  - Memory records have provenance, confidence, and verification metadata.
  - The owner can correct or remove remembered information.
  - Context assembly is deterministic, bounded, and auditable.

### Slice 7: Agency Signed HTTP Integration
- **Goal**: Make Agency a first-class durable job path.
- **Outcome**: Ariel can start Agency work, receive signed Agency events, track progress, and notify Discord.
- **Dependencies**: Slice 2, Slice 3
- **Acceptance**:
  - Agency runs are represented as Ariel jobs.
  - Agency callbacks use signed HTTP ingress with replay protection.
  - Incoming Agency events are persisted before processing.
  - Artifacts and status updates are inspectable from Discord.
  - Remote-impacting Agency actions remain approval-gated.

### Slice 8: Notifications + Scheduled Checks
- **Goal**: Deliver durable Discord notifications from jobs and scheduled reads.
- **Outcome**: Ariel can notify the owner about completed work and subscribed read-only checks.
- **Dependencies**: Slice 2, Slice 4
- **Acceptance**:
  - Notification records and delivery attempts are durable.
  - Discord delivery is deduped and retryable.
  - Quiet hours/mute policy is enforced before delivery.
  - Scheduled checks are read-only and subscription-bounded.

### Slice 9: Web/URL/Weather/Maps Hardening
- **Goal**: Keep external read integrations reliable under the new runtime.
- **Outcome**: Search, news, URL extraction, weather, and maps behave consistently in Discord and jobs.
- **Dependencies**: Slice 4
- **Acceptance**:
  - Egress remains fail-closed with explicit allowlists.
  - Large or blocked URL extraction fails clearly.
  - Maps/weather outputs remain citation/provenance ready.
  - Mixed turns preserve grounded answers and action lifecycle visibility.

### Slice 10: Google Workspace Expansion
- **Goal**: Add Drive and richer Workspace workflows after core safety is stable.
- **Outcome**: Ariel can search/read/share Drive content and support broader productivity flows.
- **Dependencies**: Slice 5
- **Acceptance**:
  - Drive search/read are allowlisted reads.
  - Drive share is approval-gated.
  - Least-privilege reconnect intent is capability-specific.
  - Permission failures are typed and recoverable.

### Slice 11: Reliability + Production Gate
- **Goal**: Make Discord-primary Ariel dependable for daily use.
- **Outcome**: The system has clear health, backups, restores, regression checks, and operational playbooks.
- **Dependencies**: Slice 7, Slice 8, Slice 9, Slice 10
- **Acceptance**:
  - Health and recent failures are inspectable.
  - Backups/restores preserve conversations, memory, jobs, agency events, notifications, and audit history.
  - Regression gates cover grounding, policy safety, tool execution, worker reliability, and Discord delivery.
  - Release checklist passes before daily-use promotion.

## deferred

### Web/PWA Frontend
- Not a primary surface and not compatibility-protected.

### Inbound MCP Control Plane
- Not part of Ariel runtime control.

### Nexus Notes Integration
- Deferred indefinitely.

### Rust Daemons
- Optional future standalone helpers only. They do not replace the FastAPI core or Postgres job/event runtime.
