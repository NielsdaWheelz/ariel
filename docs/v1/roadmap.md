# Ariel — Slice Roadmap

## Dependency Graph

```text
Slice 0: Private Walking Skeleton
    |
    v
Slice 1: Core Conversation Loop
    |
    v
Slice 2: Safe Action Framework
 /   |   \
v    v    v
Slice 3: Lightweight Read Capabilities
Slice 4: Google Workspace Core (Calendar + Email)
Slice 5: Durable Memory + Session Rotation

Slice 4 -> Slice 6: Google Workspace Expansion (Drive + Maps)
Slice 3 -> Slice 7: Web Browsing

Slice 5 -> Slice 8: Quick Capture Surface
Slice 8 -> Slice 9: Voice + Vision Interface

Slice 2 -> Slice 10: Agency Doer Integration

Slice 4 -> Slice 11A: Notification Transport Foundation
Slice 5 -> Slice 11A

Slice 4 -> Slice 11B: Proactive Notification Layer
Slice 5 -> Slice 11B
Slice 11A -> Slice 11B

Slice 11B -> Slice 12: Provider Portability + Reliability
Slice 12 -> Slice 13: Production Readiness Gate

Deferred indefinitely (unscheduled): Nexus Notes Integration
```

## Slices

### Slice 0: Private Walking Skeleton
- **Goal**: Prove a complete end-to-end user path from phone to Ariel on a private network.
- **Outcome**: A user can send a message from phone chat and receive a response from Ariel on a self-hosted instance.
- **Dependencies**: None
- **Acceptance**:
  - User reaches Ariel from phone over private access and gets a response in the same chat surface.
  - Every turn appears in an auditable timeline visible to the user.
  - Service restart does not lose prior conversation history.
- **Risks**: Private remote-access ergonomics can block daily use if setup is fragile.

### Slice 1: Core Conversation Loop
- **Goal**: Deliver natural multi-turn conversation with bounded assistant decision-making.
- **Outcome**: Ariel can hold normal back-and-forth conversation in one active session and produce coherent follow-up responses.
- **Dependencies**: Slice 0
- **Acceptance**:
  - User can send several related messages and Ariel maintains short-term continuity.
  - Ariel returns assistant messages from prompt+context (including requests for missing details when needed) without a hard-coded answer-vs-clarification state machine.
  - When turn limits are reached, user gets a clear bounded failure message instead of silent degradation.

### Slice 2: Safe Action Framework
- **Goal**: Enable tool-enabled behavior with deterministic policy and approval controls.
- **Outcome**: Ariel can propose actions, enforce approval rules, and execute only authorized actions.
- **Dependencies**: Slice 1
- **Acceptance**:
  - Read-only actions run without approval for allowlisted low-impact capabilities, and results are visible to the user with standard redaction.
  - Approval-required actions do not run before approval and run only after explicit approval.
  - Denied or expired approvals prevent execution and show a clear reason.
  - Unknown, schema-invalid, or policy-denied tool calls are blocked before execution with auditable rejection reasons.
  - Untrusted external/tool-sourced content cannot silently authorize side effects; policy must escalate to approval or deny.
  - Execution enforces capability identity/contract integrity and policy-allowed outbound destinations.
  - User can inspect what action was proposed, approved, executed, and returned.
- **Risks**: Approval UX can become too heavy or too permissive if thresholds are poorly tuned; read-capability scope and egress boundaries can be too broad if not aggressively constrained.

### Slice 3: Lightweight Read Capabilities
- **Goal**: Prove external read integrations through Ariel's safe capability framework.
- **Outcome**: User can ask factual questions, web/news queries, and weather questions in natural language.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can ask a factual question and receive a grounded answer with source references (inline citations plus structured source entries).
  - Ariel does not present externally grounded factual claims without user-visible source references; insufficient evidence is disclosed as uncertainty.
  - User can ask for weather and receive a location-aware forecast.
  - Configured default weather location is canonical user state in Postgres (with optional setup bootstrap), and lookup remains deterministic.
  - Weather location resolution is deterministic (`explicit location -> configured default -> clarification`) and does not rely on implicit IP/device geolocation.
  - User can ask for topic news and receive relevant recent results.
  - Weather execution uses a provider abstraction with an SLA-backed production backend and a local/dev fallback adapter.
  - Web/news retrieval runs through Ariel's provider-independent search capability (Brave-backed by default per constitution) rather than model-provider-locked search.
  - These reads execute without approval under read-impact policy.
  - External API failures return clear user-visible recovery guidance.
- **Risks**: Search quality variability and external API rate limits can degrade perceived reliability.

### Slice 4: Google Workspace Core (Calendar + Email)
- **Goal**: Deliver the highest-value Google productivity flows with correct safety boundaries.
- **Outcome**: User can manage schedule and email through natural language with policy-correct approvals.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can complete secure Google OAuth connect/reconnect/disconnect flows with clear connector status and recovery prompts.
  - User can retrieve schedule information and available slot options.
  - Event creation requires approval and confirms result status.
  - User can search/read email without approval.
  - User can draft email while send actions remain approval-gated.
  - Permission and consent issues are surfaced with clear recovery paths.
- **Risks**: Consent/scopes and third-party API errors can create brittle first-run UX.

### Slice 5: Durable Memory + Session Rotation
- **Goal**: Preserve continuity across sessions with durable canonical memory and user-visible projection.
- **Outcome**: Ariel rotates sessions safely while retaining validated preferences, commitments, and project context.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can start a new conversation and Ariel recalls validated preferences and commitments.
  - User can correct or remove remembered information and future behavior reflects the change.
  - Memory remains auditable and bounded rather than full historical replay.
  - User-visible memory projection remains consistent with canonical memory behavior.
- **Risks**: Stale or low-confidence memory can degrade quality if verification discipline is weak.

### Slice 6: Google Workspace Expansion (Drive + Maps)
- **Goal**: Complete key Google productivity/navigation workflows after core integration is stable.
- **Outcome**: User can find/read Drive content and request map/place information through Ariel.
- **Dependencies**: Slice 4
- **Acceptance**:
  - User can search and inspect relevant files in Drive.
  - User can ask for directions and nearby places in natural language.
  - Sharing/external-send actions remain approval-gated.
  - Permission issues are recoverable with clear guidance.
- **Risks**: Scope sprawl and API quota behavior can complicate access management.

### Slice 7: Web Browsing
- **Goal**: Add robust URL-driven research behavior on top of lightweight search.
- **Outcome**: User can provide a URL and receive extracted, summarized, source-grounded content.
- **Dependencies**: Slice 3
- **Acceptance**:
  - User can submit a URL and receive structured extracted content and summary.
  - Extraction failures (blocked pages, unsupported formats, access restrictions) are clear and actionable.
  - Large/complex pages are handled within bounded response behavior.
  - Retrieved content is represented with provenance for user inspection.
- **Risks**: Extraction reliability varies across dynamic, protected, and malformed pages.

### Slice 8: Quick Capture Surface
- **Goal**: Allow fast non-chat ingestion flows into Ariel.
- **Outcome**: User can send text/URL/content captures from phone share mechanisms into the active session.
- **Dependencies**: Slice 5
- **Acceptance**:
  - User can push quick-capture content and it appears as a normal Ariel turn.
  - Captured payloads are auditable and handled under the same policy framework.
  - Capture failures are visible with clear retry/recovery guidance.
  - Captures can feed memory workflows without bypassing policy.
- **Risks**: Input normalization and platform-specific capture behavior can be inconsistent.

### Slice 9: Voice + Vision Interface
- **Goal**: Extend the phone surface with speech and image-based interaction.
- **Outcome**: User can speak to Ariel, hear spoken responses, and send images for analysis.
- **Dependencies**: Slice 8
- **Acceptance**:
  - User can trigger push-to-talk speech input and receive speech-to-text transcript-backed turn behavior.
  - Ariel can return text-to-speech output from assistant responses.
  - User can send an image and receive useful analysis in the same conversation flow.
  - Voice/vision errors are visible and do not corrupt session state.
  - Always-on streaming voice remains out of scope for this slice.
- **Risks**: Speech and vision quality variance can impact trust without transparent fallback UX.

### Slice 10: Agency Doer Integration
- **Goal**: Let Ariel initiate and manage coding work through Agency from the same chat.
- **Outcome**: User can request coding tasks, monitor progress, and inspect outputs without leaving Ariel.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can request a coding task and receive status updates until completion.
  - User can inspect returned artifacts from completed runs in chat.
  - Actions with external or irreversible impact are approval-gated before execution.
  - Failure cases are surfaced with actionable next steps.
- **Risks**: Long-running tasks and partial failures can create confusing states without clear job visibility.

### Slice 11A: Notification Transport Foundation
- **Goal**: Establish production-grade, first-party notification delivery plumbing before proactive logic.
- **Outcome**: Ariel can deliver auditable notifications through web inbox and web push with robust preference and retry behavior.
- **Dependencies**: Slice 4, Slice 5
- **Acceptance**:
  - User can install the Ariel web app (PWA) and register/unregister web push subscriptions.
  - Service-worker-backed web push delivery works end-to-end with explicit permission and revocation handling.
  - Notification preferences (channel enablement, quiet hours, mute) are user-configurable and enforced.
  - Notification delivery records include dedupe/idempotency key, attempt status, retry/backoff metadata, and timestamps.
  - User can inspect notification history and acknowledge/dismiss events in-app.
- **Risks**: Browser/platform push behavior and permission friction can reduce delivery consistency if not handled with resilient fallback UX.

### Slice 11B: Proactive Notification Layer
- **Goal**: Deliver user-configured proactive surfacing without opening autonomous side-effect risk.
- **Outcome**: Ariel can notify users about relevant changes/events based on subscriptions and schedules.
- **Dependencies**: Slice 4, Slice 5, Slice 11A
- **Acceptance**:
  - User can configure recurring notification checks and notification preferences.
  - Ariel sends notifications for high-value events (for example schedule reminders; completed-job notifications when Slice 10 is enabled).
  - Proactive execution is read-only and policy-bounded.
  - Notification history is user-inspectable and auditable.
  - User can mute, disable, or adjust proactive behavior.
- **Risks**: Notification fatigue and delivery reliability can reduce product value if not tuned.

### Slice 12: Provider Portability + Reliability
- **Goal**: Make the assistant resilient to model/provider changes without user-facing regressions.
- **Outcome**: Core conversation, capability, and proactive flows remain stable when provider configuration changes.
- **Dependencies**: Slice 11B
- **Acceptance**:
  - Core user journeys continue to work when switching model providers.
  - Provider outages or degraded responses fail clearly without corrupting conversation or action state.
  - Decision and action traces remain inspectable across providers.
  - Provider changes do not break external knowledge capability behavior.
  - Cross-provider must-have workflow scorecards meet explicit release targets: task success >= 90%, schema-valid tool-call rate >= 99%, citation-compliance rate = 100% for externally grounded claims, and critical policy violations = 0.
- **Risks**: Behavior drift between providers can reduce predictability of plans and tool usage.

### Slice 13: Production Readiness Gate
- **Goal**: Make Ariel safe and dependable for day-to-day personal operations.
- **Outcome**: The system has operational safeguards, recoverability, and release confidence for ongoing use.
- **Dependencies**: Slice 12
- **Acceptance**:
  - User can inspect system health and recent failures.
  - Backups and restores preserve conversations, canonical memory, jobs, notifications, and audit history.
  - Common failure scenarios have clear recovery playbooks.
  - Release gates include measurable quality budgets for grounded answers, tool success, notification relevance, and multimodal UX reliability.
  - Release checklist passes before promotion to daily-use status.

### Deferred: Nexus Notes Integration (Indefinite)
- **Status**: Deferred indefinitely; not scheduled in the current slice plan.
- **Goal**: Connect Ariel to the user's notes workspace for safe note retrieval and note updates.
- **Outcome**: User can search/read/create/append notes in Nexus through chat.
- **Dependencies**: Unscheduled
- **Acceptance (when resumed)**:
  - User can retrieve relevant notes for a topic request.
  - User can ask Ariel to save or append a note and see the result in conversation flow.
  - Note writes follow reversible-write policy and remain auditable.
  - Connector/permission failures surface clear remediation guidance.
- **Risks**: External notes API semantics and conflict behavior can create sync edge cases.
