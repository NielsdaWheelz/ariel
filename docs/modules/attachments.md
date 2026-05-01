# Attachment Content Hard Cutover

## Scope

This doc owns the target architecture for turning user-supplied attachment
references into model-usable evidence. It covers Discord attachments first, but
the product surface is transport-neutral: the assistant reads an attachment, not
"a Discord CDN URL".

This doc supersedes the current metadata-only attachment behavior for any user
intent that clearly requires attachment content.

## Cutover Policy

- Ship as a hard cutover.
- Do not preserve the old behavior where Ariel can only mention attachment
  filenames, sizes, content types, or URLs when content is required.
- Do not add a legacy metadata-only fallback path.
- Do not support old request aliases or compatibility fields once the cutover
  lands.
- Do not pass Discord attachment URLs or raw bytes directly to the model as
  ambient context.
- Fail closed with a typed attachment-read outcome when acquisition, scanning,
  extraction, permission checks, or provider processing cannot complete.
- Keep metadata-only attachment context only as a pre-read reference surface,
  so the model can decide whether to call the attachment-read capability.

## Target Behavior

### User Behavior

- When the user asks Ariel to read, summarize, inspect, extract text from,
  transcribe, compare, or answer questions about an attachment, Ariel reads the
  attachment through a capability before answering.
- Answers grounded in attachment content cite the attachment source by filename,
  message context, and persisted artifact reference.
- If an attachment-only message has no actionable instruction, Ariel does not
  blindly ingest the file. It either asks what the user wants done with the
  attachment or stays silent when the ambient Discord behavior calls for no
  visible reply.
- If content cannot be read, Ariel gives a concrete outcome, such as
  `unsupported_type`, `too_large`, `expired`, `unavailable`, `unsafe`,
  `scan_failed`, `extract_failed`, or `provider_timeout`.
- Attachment content cannot cause Ariel to perform side effects by instruction
  alone. Any action influenced by attachment content remains tainted and must
  pass the existing proposal and policy path.

### System Behavior

- Discord ingress records attachment references, not content, in the normal
  message context.
- The model sees an opaque attachment reference, filename, declared content
  type, size, and source message context. It does not see the raw Discord CDN
  URL.
- The model uses one primary tool, `cap.attachment.read`, to inspect content.
- The read capability performs authorization, acquisition, validation, malware
  scanning, extraction, artifact persistence, and provenance creation.
- Extracted text, OCR, image observations, and transcripts are returned as
  bounded tool output, never as system instructions.
- The action runtime treats attachment content as untrusted file evidence and
  records retrieval provenance in the same grounded-answer path used by web and
  Drive reads.

## Goals

- Make attachments first-class evidence in Ariel, not incidental Discord
  metadata.
- Use a transport-neutral capability that can later read attachments from other
  surfaces without adding parallel product APIs.
- Keep Discord transport code responsible for delivery and reference capture
  only.
- Keep acquisition and extraction isolated from prompt assembly.
- Preserve provenance from user message to attachment blob to extracted block
  to final answer.
- Respect attachment permissions, home-guild/user boundaries, retention, and
  data minimization.
- Treat all attachment content as untrusted input.
- Bound every resource dimension: bytes, pages, image pixels, audio duration,
  wall time, extraction output, chunks, and citations.
- Make failure explicit and recoverable instead of hiding it behind generic
  model text.

## Non-Goals

- No automatic reading of every attachment the user posts.
- No live voice, voice-call, or streaming audio product surface.
- No long-term memory writes from attachment content without an explicit memory
  or capture flow.
- No arbitrary URL fetch tool exposed through attachment reading.
- No Discord CDN URL as canonical storage.
- No provider-hosted file object as Ariel's canonical record of the attachment.
- No compatibility mode for old metadata-only answer behavior.
- No broad document-management system, shared-drive sync, or full enterprise
  RAG platform in this cutover.

## Reference Model

The durable product pattern is the same across mature assistant and workplace
systems:

- Chat products expose files as explicit objects with limits, retention, and
  citations.
- Workplace assistants summarize or answer over files only within the user's
  access boundary.
- Model APIs support images, files, search, and audio, but production systems
  still wrap those primitives in authorization, storage, policy, and provenance.
- Agent safety guidance treats files, web pages, and tool results as untrusted
  data that can contain prompt injection.
- The transport URL is an acquisition handle, not an identity, permission
  model, evidence record, or citation.

## Structure

The final system is split into clear ownership layers:

- Transport reference surface: Discord captures attachment facts and Ariel
  attachment references.
- Message context surface: app prompt assembly lists available attachment refs
  without content or raw download URLs.
- Capability surface: `cap.attachment.read` is the single model-callable entry
  point for attachment inspection.
- Attachment content service: acquisition, validation, scanning, storage, and
  extraction orchestration.
- Blob and extraction stores: Ariel-owned durable evidence records.
- Modality extractors: document, image, and audio adapters with bounded outputs.
- Runtime provenance: retrieval artifacts, citations, and taint propagation.
- Policy: permission checks, side-effect gating, and memory-write prevention.

The primary data flow is:

1. Discord message arrives with attachment metadata.
2. Ariel creates an attachment reference bound to the source message and
   user/session boundary.
3. The model sees the reference and calls `cap.attachment.read` only when content
   is needed.
4. The capability resolves, authorizes, fetches, scans, stores, and extracts the
   attachment.
5. Extracted blocks return as bounded tainted tool output with source anchors.
6. The final answer cites attachment artifacts or reports a typed read failure.

## Architecture

### Layer 1: Transport Reference Capture

`src/ariel/discord_bot.py` captures attachment references from Discord messages.
The target reference shape is:

- `source`: `discord`
- `source_message_id`
- `source_channel_id`
- `source_guild_id` when present
- `source_author_id`
- `source_attachment_id`
- `filename`
- declared `content_type`
- declared `size_bytes`
- Ariel `attachment_ref`

The bot may forward the Discord download handle to the app, but the handle is
not rendered into model context or durable event text. If it must be persisted
for retry, it is stored encrypted with a short TTL and is never treated as the
attachment's identity.

### Layer 2: Message Context

`src/ariel/app.py` renders attachment context as available references:

- The model can see that an attachment exists.
- The model can see enough metadata to decide whether content reading is
  relevant.
- The model cannot see or choose an arbitrary download URL.
- Clear content-read intents are expected to produce a `cap.attachment.read`
  call before a grounded answer.

The old text-only context that lists `url=...` is removed.

### Layer 3: Attachment Read Capability

`src/ariel/capability_registry.py` exposes one capability:

- Capability id: `cap.attachment.read`
- Response tool name: generated by the existing response-tool mapping
- Input: `attachment_ref` and a narrow intent: `summarize`, `ocr`,
  `transcribe`, `extract_text`, or `answer`
- Output: typed status, normalized artifact metadata, bounded extracted blocks,
  citations, and runtime provenance

The capability accepts only attachment references produced by Ariel ingress. It
does not accept user-provided URLs or filesystem paths.

### Layer 4: Acquisition and Storage

New attachment-content code owns acquisition and persistence:

- Resolve the reference to a permitted Discord attachment handle.
- Verify the source message belongs to the active user/session boundary.
- Fetch with strict host allowlisting, redirect limits, byte limits, and
  timeout limits.
- Sniff content from bytes; extension and declared content type are advisory.
- Scan before extraction.
- Store bytes in a configured content-addressed blob store keyed by hash.
- Store metadata, source linkage, scan status, extraction status, and retention
  policy in Postgres.
- Never store raw bytes in message events, assistant context, logs, or
  retrieval summaries.

Production requires an explicit blob-store and scanner configuration. Tests can
use fakes. Development can use a local configured store, but there is no
implicit temporary-directory fallback.

### Layer 5: Modality Extractors

Attachment extraction is modality-specific and behind the capability boundary.

- Documents and text: parse text, PDFs, and supported office formats into
  bounded text blocks with page or section provenance.
- Images: run OCR and visual understanding into bounded observations, object
  descriptions, and OCR blocks with image-region provenance when available.
- Audio: transcribe supported audio files into timestamped transcript blocks
  and diarization labels when the provider supports them.
- Unsupported types return `unsupported_type`.
- Oversized or over-budget inputs return `too_large` or `resource_limit`.
- Provider failures return typed transient outcomes and do not produce partial
  uncited answers.

Provider-native multimodal input is an implementation detail of an extractor.
It is not the app architecture and is not exposed through Discord transport.

### Layer 6: Artifact and Provenance Integration

`src/ariel/action_runtime.py` treats successful attachment reads as retrieval
evidence:

- Persist a retrieval artifact for every successful read.
- Preserve the source chain from Discord message to attachment reference to blob
  hash to extracted block.
- Mark runtime provenance as tainted because attachment content is untrusted
  file content.
- Feed bounded extracted blocks back to the model as tool output.
- Require final answers to cite the artifact/source blocks they used.
- Keep taint on any later action proposal influenced by attachment content.

### Layer 7: Policy

`src/ariel/policy_engine.py` enforces the same principle already used for web,
Drive, and prior tool output:

- Attachment content is untrusted.
- Attachment content cannot grant capability permissions.
- Attachment content cannot override system, developer, user, or policy
  instructions.
- Attachment content cannot silently create memory.
- Attachment content can ground answers and proposals only with provenance.
- Side effects influenced by attachment content require approval unless an
  existing explicit policy says otherwise.

## Data Model

The final schema should separate source references, blobs, and extractions.

### Attachment Source Record

- Stable Ariel attachment id
- Source transport and source ids
- Owner user/session boundary
- Filename and declared metadata
- Opaque reference id
- Encrypted transient acquisition handle when still valid
- Retention and deletion timestamps

### Attachment Blob Record

- Blob id
- Content hash
- Storage key
- Size
- Sniffed MIME type
- Scan status and scanner version
- Created/deleted timestamps

### Attachment Extraction Record

- Extraction id
- Blob id
- Modality
- Extractor and version
- Status
- Bounded structured blocks
- Citation anchors, such as page, section, image region, or audio timestamp
- Provider request metadata without raw content

Existing `ArtifactRecord` remains the answer/provenance surface. Attachment
tables own source bytes and extraction lifecycle.

## Rules

- Do not render Discord attachment URLs into model-visible system text.
- Do not expose a generic URL-fetching attachment tool.
- Do not read an attachment unless the user intent or explicit tool call needs
  content.
- Do not answer content questions from metadata.
- Do not treat filenames, extensions, or declared content types as proof of
  modality.
- Do not extract unscanned bytes in production.
- Do not send raw file content to non-extractor paths.
- Do not write attachment-derived facts to memory unless the memory flow is
  explicitly invoked and provenance is preserved.
- Do not let attachment content choose tools, recipients, permissions, or
  policy.
- Do not keep partial legacy tests that assert `"Uploaded attachment(s)."` as a
  complete assistant behavior.
- Do not silently degrade to metadata-only answers after read failure.

## Files

Implementation should touch these files deliberately:

- `docs/modules/attachments.md`: this spec
- `docs/modules/index.md`: module-doc index
- `README.md`: product behavior summary
- `docs/production-runbook.md`: operational behavior, limits, and recovery
- `docs/modules/transport.md`: transport ownership boundary if needed
- `src/ariel/discord_bot.py`: capture attachment references
- `src/ariel/app.py`: request schema, context rendering, model loop expectations
- `src/ariel/config.py` and `.env.example`: attachment limits, store path,
  scanner mode, and extractor model settings
- `src/ariel/capability_registry.py`: `cap.attachment.read`
- `src/ariel/action_runtime.py`: retrieval artifact and taint integration
- `src/ariel/persistence.py`: attachment tables and artifact integration
- `src/ariel/attachment_content.py`: acquisition, scanning gate, blob storage,
  extraction, typed outcomes, and runtime provenance
- `alembic/versions/`: hard-cutover schema migration
- `tests/unit/test_discord_bot.py`: reference capture and no blind ingestion
- `tests/unit/test_responses_tool_contract.py`: strict tool schema
- `tests/integration/test_pr01_acceptance.py`: Discord message behavior
- New integration tests for image, document, audio, failure, and taint cases

## Key Decisions

1. Use `cap.attachment.read`, not `cap.discord.attachment.read`.
   Attachment reading is a product capability. Discord is one source.

2. Use attachment references as model inputs.
   The model should request "read this attachment ref", not fetch or reason
   over a Discord CDN URL.

3. Store blobs by content hash under Ariel control.
   Provider file ids and Discord URLs are operational handles, not canonical
   evidence records.

4. Treat multimodal provider calls as extractor internals.
   Direct image, file, or audio inputs to a model are valid implementation
   techniques, but they do not belong in transport code or ambient context.

5. Make all attachment-derived content tainted.
   The final answer can use it as evidence with citations; action proposals
   remain constrained by policy.

6. Use typed failures instead of graceful degradation.
   If content is needed and cannot be read, the user should see why.

7. Keep attachment-only messages intent-gated.
   Hard cutover does not mean blind ingestion. It means clear content intents
   cannot be satisfied by metadata-only behavior.

8. Cut over tests and docs in the same change.
   Old tests that encode metadata-only behavior are removed or rewritten.

## Acceptance Criteria

### Behavior

- A Discord message with an image attachment and "what is in this?" causes
  `cap.attachment.read` before Ariel answers.
- A Discord message with a text or PDF attachment and "summarize this" causes
  `cap.attachment.read` before Ariel answers.
- A Discord message with an audio attachment and "transcribe this" causes
  `cap.attachment.read` before Ariel answers.
- An attachment-only message with no instruction does not blindly ingest the
  attachment.
- Ariel does not answer attachment-content questions from filename, content
  type, size, or URL metadata.
- Successful attachment answers cite source artifacts.
- Failed attachment reads surface typed user-visible outcomes.

### Security

- Model-visible context never contains raw Discord attachment URLs.
- `cap.attachment.read` rejects arbitrary URLs, paths, and stale refs.
- The read path enforces user/session/source boundaries.
- Production extraction refuses unscanned bytes.
- MIME sniffing controls extractor selection.
- Attachment content appears in tool output or artifacts, not system messages.
- Tainted attachment content cannot silently trigger side effects or memory
  writes.

### Persistence

- Attachment source records, blob records, and extraction records are distinct.
- Blobs are content-addressed.
- Retrieval artifacts preserve source-chain provenance.
- Logs and events do not contain raw attachment bytes or full extracted content
  beyond intended bounded artifacts.
- Retention and deletion paths cover source records, blobs, and extractions.

### Testing

- Unit tests cover Discord reference capture without URL rendering.
- Unit tests cover strict `cap.attachment.read` schema mapping.
- Unit tests cover unsupported, too-large, expired, unavailable, unsafe,
  scan-failed, extraction-failed, and timeout outcomes.
- Integration tests cover image, document, and audio success paths.
- Integration tests cover prompt-injection text embedded inside an attachment.
- Integration tests cover side-effect proposals influenced by attachment
  content remaining tainted.
- Existing metadata-only attachment assertions are removed or rewritten.
- `make verify` passes after the cutover branch is complete.

## Implementation Plan

1. Add the schema and persistence layer.
   Create source, blob, and extraction records with a hard-cutover migration and
   no compatibility aliases.

2. Add the attachment-content service.
   Implement reference resolution, authorization, acquisition, byte sniffing,
   scanning, blob storage, extraction dispatch, and typed outcomes.

3. Add modality extractors.
   Start with text/PDF, images/OCR/vision, and audio transcription because
   those match the product promise. Keep each extractor bounded and versioned.

4. Add `cap.attachment.read`.
   Register the strict tool schema, input normalization, execution path, and
   response-tool mapping.

5. Cut over Discord ingress and app context.
   Replace raw URL rendering with opaque attachment refs and update the model
   context so content-read intents call the capability.

6. Wire runtime provenance.
   Persist retrieval artifacts, return bounded content blocks, cite sources,
   and propagate taint into later proposals.

7. Rewrite tests and docs.
   Remove metadata-only expectations, add success/failure/security coverage,
   and update README/runbook behavior.

8. Run verification.
   Run focused tests first, then full verification. Fix blockers in the
   cutover branch instead of adding fallback behavior.

## Final State

Ariel can receive Discord attachments as references, read supported image,
document, and audio content through one capability, persist evidence under
Ariel-owned provenance, and answer with citations. The Discord transport stays
thin. The model never sees raw Discord attachment URLs as ambient context.
Attachment content remains untrusted. Clear content-read requests either produce
a grounded answer or a typed failure. The old metadata-only behavior is gone.
