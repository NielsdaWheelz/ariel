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
   / \
  v   v
Slice 3: Agency Doer Integration    Slice 4: Calendar Assistant
   \                                   /
    \                                 /
     v                               v
      Slice 5: Durable Memory + Session Rotation
                    |
                    v
       Slice 6: Provider Portability + Reliability
                    |
                    v
         Slice 7: Production Readiness Gate
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
  - Ariel can decide to answer directly or ask a clarifying question when intent is ambiguous.
  - When turn limits are reached, user gets a clear bounded failure message instead of silent degradation.

### Slice 2: Safe Action Framework
- **Goal**: Enable tool-enabled behavior with deterministic policy and approval controls.
- **Outcome**: Ariel can propose actions, enforce approval rules, and execute only authorized actions.
- **Dependencies**: Slice 1
- **Acceptance**:
  - Read-only actions run without approval and results are visible to the user.
  - Approval-required actions do not run before approval and run only after explicit approval.
  - Denied or expired approvals prevent execution and show a clear reason.
  - User can inspect what action was proposed, approved, executed, and returned.
- **Risks**: Approval UX can become too heavy or too permissive if thresholds are poorly tuned.

### Slice 3: Agency Doer Integration
- **Goal**: Let Ariel initiate and manage coding work through Agency from the same chat.
- **Outcome**: User can request coding tasks, monitor progress, and inspect outputs without leaving Ariel.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can request a coding task and receive status updates until completion.
  - User can inspect returned artifacts from completed runs in chat.
  - Actions with external or irreversible impact are approval-gated before execution.
  - Failure cases are surfaced with actionable next steps.
- **Risks**: Long-running tasks and partial failures can create confusing states without clear job visibility.

### Slice 4: Calendar Assistant
- **Goal**: Provide safe schedule awareness and scheduling actions.
- **Outcome**: User can ask what is upcoming, request slot proposals, and create events with approval.
- **Dependencies**: Slice 2
- **Acceptance**:
  - User can retrieve upcoming schedule information in natural language.
  - User can request available time proposals under constraints and receive options.
  - User can create an event only through an approval step and confirm the result.
  - Permission issues are reported with clear recovery guidance.

### Slice 5: Durable Memory + Session Rotation
- **Goal**: Preserve continuity across new conversations without relying on one unbounded thread.
- **Outcome**: Ariel starts fresh sessions while retaining relevant durable memory and active commitments.
- **Dependencies**: Slice 3, Slice 4
- **Acceptance**:
  - User can start a new conversation and Ariel still recalls validated preferences and commitments.
  - User can correct or remove remembered information and future behavior reflects that change.
  - Ariel keeps current-session flow natural without replaying full historical transcripts.
- **Risks**: Memory quality can regress if stale or low-confidence memories are not controlled.

### Slice 6: Provider Portability + Reliability
- **Goal**: Make the assistant resilient to model/provider changes without user-facing regressions.
- **Outcome**: Core conversation and action flows remain stable when provider configuration changes.
- **Dependencies**: Slice 5
- **Acceptance**:
  - Core user journeys continue to work when switching model providers.
  - Provider outages or degraded responses fail clearly without corrupting conversation or action state.
  - Decision and action traces remain inspectable across providers.
- **Risks**: Behavior drift between providers can reduce predictability of plans and tool usage.

### Slice 7: Production Readiness Gate
- **Goal**: Make Ariel safe and dependable for day-to-day personal operations.
- **Outcome**: The system has operational safeguards, recoverability, and release confidence for ongoing use.
- **Dependencies**: Slice 6
- **Acceptance**:
  - User can inspect system health and recent failures.
  - Backups and restores preserve conversations, memory, jobs, and audit history.
  - Common failure scenarios have clear recovery playbooks.
  - Release checklist passes before promotion to daily-use status.
