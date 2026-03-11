# Slice 8: Quick Capture Surface — PR Roadmap

### PR-01: Capture Ingress Vertical (`POST /v1/captures`) for Text + URL
- **goal**: deliver first-class quick capture for note/text/url shares with durable capture identity, server-resolved session targeting, and replay-safe turn creation through Ariel’s existing conversation runtime.
- **builds on**: Slice 7 PR-01 merged state (typed surfaced turn/artifact contracts, retrieval provenance, and taint-aware action runtime) and Slice 5 PR-02 merged state (one-active-session rotation and idempotent message ingress).
- **acceptance**:
  - Ariel introduces authenticated `POST /v1/captures` for bounded `text` and `url` captures with optional note/source metadata and no client-supplied session id.
  - accepted capture submissions persist durable `cpt_` identity, original payload, normalized turn input, effective session id, and terminal linkage to exactly one created turn or one typed ingest failure.
  - successful captures resolve the effective active session server-side, execute through the same turn/orchestration/action-lifecycle path as normal chat turns, and appear in existing session timeline/message surfaces rather than a separate conversation history.
  - capture idempotency is request-scoped across retries and session rotation: identical replays return the original capture/turn outcome, while conflicting payload reuse returns a typed idempotency conflict without duplicate turns.
  - invalid, unsupported, or oversize capture payloads fail before turn creation with typed recovery guidance and durable capture status instead of silent drop behavior.
  - bare text/url captures are observe-first input only: they do not implicitly authorize writes, approvals, or direct memory mutation outside the normal turn/policy path.
- **non-goals**: shared text-content captures with explicit note/source separation; capture-origin taint hardening for shared source bodies; any Ariel-owned capture-entry UX; image/audio/file capture; offline/background client queues.

### PR-02: (planned after PR-01 merges) Shared-Content Capture Hardening + Source/Policy Safety
- **goal**: complete Slice 8 by hardening shared-content capture semantics and inspection/failure behavior so future share clients can use the capture ingress safely without opening a side channel around policy or memory.
- **builds on**: PR-01.
- **acceptance**:
  - `POST /v1/captures` accepts shared text-content payloads and preserves explicit separation between user-authored note and shared source material while retaining raw capture payload for audit and future multimodal extension.
  - capture-origin shared source material is treated as untrusted ingress provenance, so side-effecting proposals influenced by it escalate or deny under Ariel’s existing taint and approval rules instead of auto-authorizing.
  - observe-first behavior is preserved for bare shared content: source text/URLs without explicit user instruction are conversational context, not direct commands, approvals, or memory instructions.
  - capture outcomes distinguish ingress rejection from in-turn failure in surfaced capture contracts while linking successful captures back to the normal turn/timeline surfaces.
  - capture turns can participate in browsing/retrieval and memory workflows without bypassing citation/provenance requirements, approval policy, or candidate/validated memory lifecycle rules.
  - regression coverage blocks release on capture-specific invariants: capture-scoped idempotency across rotation, note/source separation, taint-driven side-effect blocking, durable failure classification, and no direct capture-to-memory side channel.
- **non-goals**: chat-page capture integration, native mobile share-target UI, binary/vision/speech capture, batched capture submission, or proactive/background clipping.
