# Slice 3: Lightweight Read Capabilities — PR Roadmap

### PR-01: Grounded Retrieval Core (Web Search + Provenance Artifacts + Citation-Gated Answers)
- **goal**: deliver the first full Slice 3 vertical path for externally grounded factual answers using capability-mediated retrieval, durable provenance artifacts, and citation-gated response synthesis.
- **builds on**: Slice 2 PR-08 merged state (safe action runtime, policy/approval, surfaced lifecycle contracts, egress preflight). Current codebase has probe-only capabilities, single-pass assistant output, and no provenance artifact store/API.
- **acceptance**:
  - when a factual query needs external evidence, Ariel executes `cap.search.web` as a `read` capability under policy allowlist (no approval), with auditable action lifecycle and explicit egress allowlist enforcement.
  - Ariel returns a synthesized final answer with user-visible citation references (inline markers) plus a structured citation contract (`assistant.sources[]` with artifact ids), rather than only appending raw tool output text.
  - each cited source is persisted as an inspectable provenance artifact with stable identity and citation metadata (at minimum title, source URL/handle, retrieval time, publication time when present), and cited artifacts can be fetched from a user-facing artifact endpoint.
  - Ariel does not present unsupported external factual claims as true; when evidence is missing or conflicting, Ariel returns explicit uncertainty and a concrete recovery step.
  - retrieval timeout/rate-limit/upstream failure paths are explicit in user-visible responses, mark partial outputs as partial, and remain reconstructable in action/event history.
- **non-goals**: no topic-news ranking/freshness tuning beyond baseline retrieval plumbing; no weather-specific location resolution behavior; no URL extraction/full-page parsing workflows.

### PR-02: News + Weather Behaviors (planned after PR-01 merges)
- **goal**: complete Slice 3 by adding production-grade news and weather user journeys on top of PR-01 grounded-retrieval/citation foundations.
- **builds on**: PR-01.
- **acceptance**:
  - topic news requests execute via `cap.search.news` (not provider-native search shortcuts) and return relevant recent items with publication timestamps and user-visible source references.
  - Ariel discloses stale or ambiguous recency conditions instead of presenting stale news as current without warning.
  - weather requests execute via provider-abstracted `cap.weather.forecast` (SLA-backed production backend, non-SLA dev fallback adapter) with deterministic location resolution (`explicit location -> configured default -> clarification`) and never infer location from implicit IP/device signals.
  - configured weather default location is canonical user state in Postgres (optionally initialized from env bootstrap), and resolution behavior remains deterministic.
  - when weather location cannot be resolved, Ariel asks a location clarification instead of guessing.
  - weather responses include resolved location/timeframe, forecast or observation timestamps, and source references; upstream weather/search failures remain explicit and recoverable.
  - regression coverage demonstrates end-to-end Slice 3 behavior across grounded factual Q&A, news recency/attribution, weather location handling, and failure recovery.
- **non-goals**: no proactive scheduler/notification execution, no durable-memory/session-rotation changes, no provider failover guarantees beyond capability-mediated retrieval architecture.

### PR-03: Grounding Safety Hardening (planned after PR-02 merges)
- **goal**: close remaining Slice 3 grounding safety gaps so external factual responses stay citation-gated even under conflicting evidence and mixed proposal sets.
- **builds on**: PR-02.
- **acceptance**:
  - when retrieval evidence is conflicting for the same factual claim, Ariel returns explicit uncertainty plus a concrete recovery step instead of presenting a definitive claim.
  - turns that include retrieval (`cap.search.web`/`cap.search.news`/`cap.weather.forecast`) still produce grounded synthesis with inline citations + `assistant.sources[]`, even when mixed with non-retrieval proposals.
  - external factual assertions in surfaced assistant text are blocked unless they are backed by cited provenance artifacts; unsupported assertions are replaced with uncertainty language.
  - non-retrieval action outcomes remain inspectable in the same turn response flow without regressing lifecycle/event auditability.
  - regression coverage adds explicit conflict-evidence and mixed-turn citation-gating cases and blocks release on failure.
- **non-goals**: no full claim-extraction/NLI platform, no cross-turn truth maintenance, and no ranking/relevance ML changes beyond deterministic MVP heuristics.
