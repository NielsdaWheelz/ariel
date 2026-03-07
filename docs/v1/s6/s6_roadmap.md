# Slice 6: Google Workspace Expansion (Drive + Maps) — PR Roadmap

### PR-01: Drive Vertical (Search/Read/Share) + OAuth Scope Expansion
- **goal**: deliver a production-safe Drive vertical slice with natural-language file discovery/inspection plus approval-gated sharing under existing policy and audit contracts.
- **builds on**: Slice 4 PR-03 merged state (Google connector/readiness foundations) and Slice 5 PR-01 merged state (current orchestration/response contracts).
- **acceptance**:
  - Ariel introduces `cap.drive.search` and `cap.drive.read` as allowlisted `read` capabilities, and `cap.drive.share` as `external_send` with approval required.
  - Drive reconnect intent is capability-scoped and least-privilege (incremental scopes for Drive operations) while preserving existing connector semantics and typed auth/scope failures.
  - Drive read/search results use normalized retrieval output with provenance artifacts/citations, preserving grounded-answer synthesis behavior rather than raw provider payload passthrough.
  - Drive read is bounded and format-safe by default: unsupported/too-large/unavailable content paths are explicit typed outcomes with clear user recovery guidance.
  - Drive share executes only after approval and only for the exact approved payload hash; deny/expire/mismatch paths remain blocked and auditable.
  - egress declarations/destination allowlists, execution integrity checks, redaction, and action lifecycle visibility remain intact for all new Drive capabilities.
- **non-goals**: Maps capabilities; Drive upload/create/edit/delete/move workflows; full-fidelity rendering for all binary/Google-native document types.

### PR-02: (planned after PR-01 merges) Maps Read Vertical + Service-Credential Reliability
- **goal**: deliver maps directions and nearby-place workflows as policy-safe read capabilities with robust typed failures and deterministic clarification behavior.
- **builds on**: PR-01.
- **acceptance**:
  - Ariel introduces `cap.maps.search_places` and `cap.maps.directions` as allowlisted `read` capabilities with explicit egress allowlists and no approval path.
  - Maps execution uses restricted server-side provider credentials (secret-safe handling, no user reconnect consent loop), with typed recoverable behavior for missing/invalid credential configuration.
  - Directions/place outputs follow normalized retrieval contracts so citations/provenance artifacts remain user-visible and compatible with existing grounded synthesis.
  - Route/place requests requiring missing origin/location details trigger explicit clarification rather than implicit IP/device geolocation inference.
  - Provider permission/quota/rate-limit/transient failures surface typed, user-actionable recovery guidance and auditable lifecycle outcomes without mutating Google OAuth connector readiness semantics.
  - regression coverage validates mixed retrieval turns (Drive/Maps/web/news), typed failure contracts, and policy invariants.
- **non-goals**: traffic-aware optimization, multi-stop route planning, commute learning/personalization, or proactive maps-triggered notifications.
