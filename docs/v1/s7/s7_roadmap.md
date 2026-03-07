# Slice 7: Web Browsing — PR Roadmap

### PR-01: URL Extraction Vertical (`cap.web.extract`) + Safety/Reliability Hardening
- **goal**: deliver Slice 7 end-to-end with production-safe URL extraction, grounded responses, strict safety preflight, and release-gated reliability behavior.
- **builds on**: Slice 6 PR-02 merged state (retrieval synthesis, provenance artifacts, typed capability runtime guardrails, and fail-closed egress preflight).
- **acceptance**:
  - Ariel introduces `cap.web.extract` as an allowlisted `read` capability with explicit egress declaration/allowlist behavior and no approval path.
  - user URL requests execute through capability-mediated extraction (not model-vendor native browsing shortcuts) and return structured extracted content plus grounded assistant response with inline citations and synchronized `assistant.sources[]`.
  - URL safety preflight is strict and fail-closed before extraction execution (including invalid/non-http(s) rejection and unsafe destination protection posture).
  - successful extraction persists inspectable provenance artifacts for cited URL evidence, with stable source identity and retrieval timing available via existing artifact surfaces.
  - canonical source identity remains stable across redirects/retries so provenance and citations are dedupe-safe and auditable.
  - invalid/restricted/unsupported/provider/runtime extraction failures surface a complete typed failure taxonomy with actionable recovery guidance and auditable lifecycle outcomes.
  - large/complex page handling is bounded and explicit (truncation/partial coverage surfaced clearly rather than silent timeout/degradation).
  - transient provider failures use bounded retry/backoff behavior without violating turn/runtime budgets or producing duplicate user-visible artifacts.
  - mixed turns containing `cap.web.extract` plus non-retrieval proposals keep retrieval-grounded assistant messaging while preserving structured lifecycle inspectability for all proposals.
  - regression coverage blocks release on browsing-specific safety and quality invariants: URL safety policy enforcement, bounded extraction behavior, citation/provenance correctness, and mixed-turn grounding integrity.
- **non-goals**: authenticated/sessioned browsing flows, paywall/CAPTCHA bypass, browser automation for side-effecting actions, multi-hop crawling autonomy, non-text-first media extraction, or proactive/background URL monitoring.
