# Slice 8: Quick Capture Surface — Spec

## Goal

Allow fast non-chat ingestion flows into Ariel.

## Acceptance Criteria

### quick capture lands as a normal Ariel turn in the effective active session
- **given**: an authenticated user submits a supported capture from a phone share mechanism while Ariel has zero or one active session
- **when**: Ariel accepts the capture
- **then**: Ariel resolves the effective active session server-side (creating or rotating deterministically if needed), creates exactly one normal turn for that capture in that session, and exposes the result through the existing message/timeline surfaces with the same assistant and action-lifecycle contracts as chat-submitted turns

### capture normalization preserves user intent and source context
- **given**: a capture containing text, a URL, or shared text content with optional source metadata and an optional user note
- **when**: Ariel ingests the payload
- **then**: Ariel stores the original capture under a stable capture id, derives a bounded normalized turn input from it, and preserves explicit separation between the user-authored note and shared source material so later audit/provenance does not depend on client-side prompt shaping

### share-flow retries are replay-safe even across session changes
- **given**: a phone capture submission is retried because delivery status is ambiguous or the active session changes during processing
- **when**: the same capture is resubmitted with the same idempotency key
- **then**: Ariel returns the same capture/turn outcome exactly once, and conflicting reuse of that key with different payload content is rejected with a typed error instead of creating duplicate turns

### capture processing stays inside normal policy and approval boundaries
- **given**: a captured URL/content would cause Ariel to read external content, propose a side effect, or touch memory
- **when**: Ariel processes the resulting turn
- **then**: the same capability policy, approval, redaction, egress, and audit rules apply as for normal chat turns, captured external material is treated as untrusted ingress context, and the capture path cannot directly authorize side effects or bypass approval

### capture failures are durable, typed, and recoverable
- **given**: a capture payload is invalid, unsupported, over budget, or the resulting turn cannot complete successfully
- **when**: Ariel handles the capture
- **then**: the failure is surfaced with a stable failure class and clear retry/recovery guidance, the capture is not silently dropped, and the user can inspect whether the failure happened before turn creation or within normal turn execution

### captures can feed memory workflows without becoming a memory side channel
- **given**: a capture contains information that Ariel may later remember, correct, or retract
- **when**: the capture turn is processed
- **then**: any memory creation, promotion, correction, or retraction follows the existing auditable conversation-mediated memory rules, and raw capture metadata cannot mutate canonical memory on its own

## Key Decisions

**`POST /v1/captures` is a first-class ingress, not an alias for `/message`**: Ariel introduces durable capture records with `cpt_` identity and terminal linkage to exactly one effective session/turn outcome or one terminal failure. User-facing conversation history remains turn-based, but capture acceptance/retry/failure is not ephemeral.

**Active-session targeting is server-resolved**: capture clients do not choose a session id. Ariel resolves the current active session at acceptance time and applies the same deterministic session-rotation rules as normal turns so share flows stay simple and the one-active-session invariant remains intact.

**Capture ingress uses a typed envelope with deterministic normalization**: quick capture accepts explicit capture kinds plus bounded metadata and derives a normalized assistant-facing turn input from that envelope. Ariel does not treat opaque client-composed prompt text as the long-term contract for capture semantics.

**Raw capture payload and normalized turn input are separate artifacts**: Ariel retains the original submitted payload for audit, replay, and future extensibility while feeding a bounded normalized representation into the turn engine. This avoids lossy conversion and keeps future multimodal capture extensible without redefining turn semantics.

**User-authored note and shared source material remain distinct**: explicit user note carries intent; shared text/URL content is preserved as referenced source context only. Shared payload cannot masquerade as direct commands, approvals, or memory instructions.

**Quick capture reuses the existing turn engine end to end**: after acceptance/normalization, capture processing runs through the same orchestration, capability proposal, approval, redaction, egress, and surfaced response machinery as `POST /v1/sessions/{session_id}/message`. There is no capture-specific tool executor or approval bypass path.

**Idempotency is capture-scoped rather than session-scoped**: replay protection is keyed to the capture request itself so share-sheet retries remain safe even if auto-rotation or active-session changes occur between attempts.

**Captured external content extends Ariel’s untrusted provenance model**: source material arriving through quick capture is treated the same way Ariel treats other untrusted external content, so any side-effecting proposal influenced by that material must escalate or deny under the existing taint rules instead of auto-authorizing.

**Bare captures are observe-first by default**: a shared URL or text payload without an explicit user instruction is informational context for the conversation, not implicit authorization to save, send, schedule, subscribe, or otherwise take action.

**Failure semantics distinguish ingress rejection from turn failure**: Ariel classifies invalid/unsupported capture intake separately from failures that occur after a turn has been created. Both paths are durable and user-visible, with stable recovery guidance rather than generic silent failure.

**Capture remains text-first in Slice 8**: MVP capture supports text, URLs, and shared text-content payloads only. Images, audio, video, binary files, OCR, and speech/vision preprocessing extend the same ingress model later rather than forcing Slice 8 to invent premature attachment semantics.

**Memory remains canonical and conversation-mediated**: captures may trigger the same remembered/corrected/forgotten outcomes as chat turns, but capture metadata never writes directly into canonical memory and cannot bypass candidate/validated lifecycle rules.

**Surface history stays unified**: successful captures appear in the existing session timeline and turn surfaces, with capture-specific identity and metadata linked for auditability instead of creating a second conversation-history model.

## Out of Scope

- Image, audio, video, and binary-file capture, including OCR/transcription and vision analysis (-> Slice 9)
- Native mobile-app-specific background queues or offline capture sync
- Multi-item or batched capture submission in one request
- Direct memory CRUD or automatic memory mutation from raw capture metadata
- Authenticated/paywalled web clipping, browser automation, or background monitoring beyond normal URL-driven browsing
- Public or multi-user share destinations
