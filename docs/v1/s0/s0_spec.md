# Slice 0: Private Walking Skeleton — Spec

## Goal

Prove a complete end-to-end user path from phone to Ariel on a private network.

## Acceptance Criteria

### phone user reaches private Ariel chat and gets a response
- **given**: a self-hosted Ariel instance is running and reachable on the user tailnet
- **when**: the user opens the phone chat surface and sends a message
- **then**: Ariel returns a response in the same chat surface for that turn using the configured model provider

### turn history is auditable in a user-visible timeline
- **given**: at least one completed turn exists in the active session
- **when**: the user opens the session timeline
- **then**: the user can see an ordered event chain for each turn, including a terminal success or failure state

### model step is visible in the timeline
- **given**: a user message triggers response generation
- **when**: Ariel processes the turn
- **then**: the timeline includes model-call start/end events for that turn, with provider/model identity, duration, token/usage metadata when available, and any failure reason so the assistant response is auditable against that model step

### conversation history survives service restart
- **given**: prior turns exist for the active session
- **when**: the Ariel service is restarted and the user reconnects from phone
- **then**: prior conversation history and timeline events remain visible, and new turns append to the same durable history

### no public ingress exists for the MVP surface
- **given**: Ariel is deployed for Slice 0
- **when**: the user accesses Ariel from phone
- **then**: access is available through private network routing only, with no public internet endpoint required

## Key Decisions

**Private-only network boundary from day one**: The service remains local/private and is exposed only through tailnet access. This locks in the MVP trust model early and avoids later migration away from public ingress assumptions.

**Event log is the source of truth for chat auditability**: Each turn writes an append-only, ordered event chain, and the user-visible timeline is rendered from that same chain. This prevents divergence between "chat history" and "audit history."

**Persistence-first walking skeleton**: Sessions, turns, and timeline events are durable in Postgres from Slice 0. In-memory-only conversation state is not acceptable for user-visible history.

**Single active session model**: Slice 0 keeps exactly one active session for the user and appends turns into it across restarts. Session rotation and durable memory retrieval are deferred to later slices.

**Model-backed minimal turn engine**: Slice 0 implements a thin turn lifecycle (intake, single model call, response generation, completion/failure logging). Capability execution, approval workflows, and long-running jobs are not included.

**Full-fidelity model observability from day one**: User-visible timeline events for model calls include enough metadata to inspect behavior and cost signals early, rather than adding this telemetry in a later slice.

## Out of Scope

- Natural multi-turn reasoning behavior, missing-details interaction behavior, and turn-budget handling (-> Slice 1)
- Capability policy enforcement and approval-gated execution (-> Slice 2)
- Agency task execution and artifact workflows (-> Slice 6)
- Calendar read/propose/create flows (-> Slice 4)
- Durable cross-session memory retrieval and session rotation policy (-> Slice 7)
- Provider failover/portability hardening and provider switching guarantees (-> Slice 13)
- Production readiness operations (health gate, backup/restore playbooks, release gate) (-> Slice 14)
- Public internet hosting, multi-user tenancy, and autonomous background actions
