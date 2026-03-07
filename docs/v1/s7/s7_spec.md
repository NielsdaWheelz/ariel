# Slice 7: Web Browsing — Spec

## Goal

Add robust URL-driven research behavior on top of lightweight search.

## Acceptance Criteria

### url submission returns structured extraction plus grounded summary
- **given**: an active session and a reachable URL to publicly accessible content
- **when**: the user asks Ariel to read/summarize that URL
- **then**: Ariel executes URL extraction as a `read` capability without approval, returns a user-facing summary grounded in extracted evidence with citations, and surfaces structured extracted content (bounded text blocks plus document metadata) in the turn lifecycle for inspection

### url eligibility and safety checks fail closed before extraction
- **given**: a URL that is invalid, non-http(s), or not eligible under Ariel’s browsing safety policy
- **when**: Ariel evaluates the extraction proposal
- **then**: Ariel blocks execution before outbound retrieval, returns a typed failure reason with a clear correction step, and does not attempt silent fallback browsing or unsafe policy bypasses

### blocked, restricted, and unsupported pages are explicit and recoverable
- **given**: a URL that is blocked, access-restricted, robot-gated, dynamically unreadable in MVP, or unsupported by the extractor
- **when**: extraction cannot produce usable text evidence
- **then**: Ariel returns an explicit failure class (for example `access_restricted`, `unsupported_format`, `provider_timeout`, `provider_upstream_failure`) with actionable recovery guidance, records auditable lifecycle status, and does not present fabricated or uncited claims as extracted facts

### large or complex pages stay bounded with partial-disclosure behavior
- **given**: a page whose size/complexity exceeds extraction or response budgets
- **when**: Ariel performs extraction and synthesis
- **then**: Ariel returns bounded structured content and a bounded summary, marks truncation/partial coverage explicitly, and suggests a narrowing strategy (for example section focus or smaller scope URL) instead of timing out silently

### extracted content remains provenance-backed and inspectable
- **given**: a successful URL extraction
- **when**: Ariel returns the response
- **then**: cited source references map to durable provenance artifacts tied to canonical/final source identity, extraction timing, and stable artifact ids, and users can inspect source metadata through existing artifact surfaces

### mixed turns preserve retrieval-grounded response behavior
- **given**: a turn that includes URL extraction plus other proposals
- **when**: Ariel finalizes the assistant response
- **then**: assistant text remains retrieval-grounded with citation markers and synchronized structured sources, while non-retrieval proposal outcomes remain inspectable through lifecycle/event surfaces

## Key Decisions

**`cap.web.extract` is a first-class retrieval capability**: URL browsing is implemented as a dedicated typed `read` capability in the same policy/action runtime as search/news/weather, not as ad-hoc model-side browsing behavior.

**Provider-mediated extraction preserves egress and portability boundaries**: Ariel sends target URLs to a bounded extraction provider interface (swappable adapter model) rather than allowing arbitrary direct crawler egress from orchestration runtime, keeping destination controls and provider portability consistent with constitution constraints.

**URL safety preflight is strict and fail-closed**: URL extraction is gated by deterministic eligibility checks (scheme and destination policy, redirect safety, and private-network protection posture) before execution.

**Extraction pipeline is deterministic and bounded**: URL browsing follows a fixed core path (`fetch -> parse/render -> content extraction -> bounded normalization -> synthesis input`) so behavior remains auditable and stable across providers.

**Extraction output follows a dual contract**: The capability returns (1) structured extracted document content for user/runtime inspection and (2) retrieval-normalized citation candidates for grounded synthesis, so URL summaries integrate with existing citation/provenance response contracts.

**Outcome taxonomy is typed and deterministic**: Extraction outcomes are explicitly classified (success, partial, blocked/restricted, unsupported, provider/runtime failure) with stable recovery guidance; Ariel does not collapse materially different failure classes into generic “failed” messaging.

**Budgeted extraction is mandatory**: Fetch, parse, and surfaced-content budgets are enforced deterministically with explicit truncation/partial indicators so complex pages remain safe within turn/runtime limits.

**URL normalization and canonical-source handling are fixed invariants**: MVP extraction uses strict URL validation/canonicalization and records canonical source identity after redirects, so citations/provenance remain stable and dedupe-safe across retries.

**Grounding policy extends to URL summaries**: Externally grounded statements derived from URL extraction must remain citation-backed and uncertainty-disclosed when evidence is insufficient or conflicting, matching existing retrieval safety behavior.

**Provenance captures extraction identity, not just display fields**: Provenance artifacts for extracted URLs retain stable source identity and extraction metadata sufficient for auditability and replay-safe comparison across retries/provider updates.

**Reliability controls are part of the capability contract**: URL extraction uses deterministic idempotency, retry/backoff discipline for transient failures, and bounded degradation behavior for partial results instead of silent quality collapse.

**Release quality gates include browsing-specific regressions**: Slice 7 changes are held by regression coverage for URL safety-policy enforcement, typed failure surfacing, bounded extraction behavior, and citation/provenance correctness.

## Out of Scope

- Authenticated/sessioned browsing workflows (login, cookie replay, paywall bypass, CAPTCHA solving)
- Open-ended multi-hop crawling or autonomous “keep browsing until done” behavior
- Full fidelity extraction for arbitrary binary/media formats (audio/video/images as primary source) beyond text-oriented MVP extraction
- Side-effecting web actions (form submission, posting, purchases, account changes)
- Proactive/background URL monitoring and push alerting (-> Slice 11B)
