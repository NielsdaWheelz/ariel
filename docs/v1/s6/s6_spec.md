# Slice 6: Google Workspace Expansion (Drive + Maps) — Spec

## Goal

Complete key Google productivity/navigation workflows after core integration is stable.

## Acceptance Criteria

### drive search returns relevant file candidates as read behavior
- **given**: a connected Google account with Drive read access and an active session
- **when**: the user asks Ariel to find files by topic/name in natural language
- **then**: Ariel executes Drive search as `read` behavior without approval, returns relevant file results with enough metadata to disambiguate candidates, and records auditable action lifecycle events

### drive inspect returns bounded readable content with provenance
- **given**: a Drive file the user can access
- **when**: the user asks Ariel to inspect/read that file
- **then**: Ariel returns a bounded, user-meaningful content view (not raw unbounded payload), includes user-visible source/provenance references to the file inspected, and clearly reports typed outcomes when content is unsupported, too large, or unavailable

### directions requests resolve route answers without implicit geolocation
- **given**: a maps request that includes or can clarify origin/destination context
- **when**: the user asks for directions in natural language
- **then**: Ariel returns route guidance grounded in map provider results, asks clarification when required route inputs are missing, and does not infer location from implicit device/IP geolocation

### nearby-place requests return useful place candidates
- **given**: a location context and a nearby-place intent (for example coffee, pharmacy, gas)
- **when**: the user asks for nearby places
- **then**: Ariel returns relevant place candidates with user-inspectable source context and clear uncertainty when provider evidence is insufficient

### drive sharing remains approval-gated external send
- **given**: a valid Drive sharing target and permission payload
- **when**: Ariel proposes a share action
- **then**: execution remains `external_send`, does not run before approval, and after approval executes only the exact approved payload once with clear success/failure status

### auth, scope, and provider failures are typed and recoverable
- **given**: missing Drive connection/consent/scope/token state, Maps provider credential/config failure, upstream permission denial, or quota/rate-limit behavior
- **when**: Ariel attempts capability execution
- **then**: Ariel returns a typed failure with explicit recovery guidance, records the reason in auditable lifecycle state, and does not silently downgrade to unsafe fallback behavior

## Key Decisions

**Capability surface is intentionally narrow for Slice 6**: This slice adds `cap.drive.search`, `cap.drive.read`, `cap.maps.directions`, and `cap.maps.search_places` as core read flows, plus `cap.drive.share` as the only external-send action in scope.

**Auth boundaries are explicit by domain**: Drive capabilities use the existing user OAuth connector lifecycle with incremental least-privilege scope upgrades. Maps capabilities use restricted server-side provider credentials and egress policy controls, not user reconnect consent loops.

**Read-output contract stays retrieval-native and citation-friendly**: Drive/Maps read capabilities return normalized retrieval-style outputs suitable for grounded answer synthesis and user-visible provenance, rather than raw provider payload passthrough.

**Drive read is bounded and format-safe by default**: MVP Drive inspection prioritizes bounded text-readable extraction with explicit limits (content class, size, and budget) and typed unsupported/too-large outcomes; Ariel does not promise full-fidelity rendering of every binary/complex file format in this slice.

**Approval boundary is hard for sharing**: `cap.drive.share` is modeled as `external_send` and inherits exact-payload hash approval semantics, preserving one-time execution and auditable approval linkage.

**Failure taxonomy remains deterministic across domains**: Drive auth/scope blocking failures map to reconnect-required behavior; Maps credential/config failures surface explicit operator/user recovery guidance; transient upstream/quota failures remain retry-oriented and do not silently mutate approval or readiness semantics.

**Egress and policy constraints remain explicit per capability**: Each new capability declares explicit allowed destinations and runs under existing schema/policy/runtime guardrails so Drive/Maps expansion does not widen outbound trust by accident.

## Out of Scope

- Drive uploads, file creation/edit/delete/move, and broader file-management workflows
- Automated or approval-bypassed sharing/external delivery behavior
- Advanced mapping workflows (multi-stop optimization, live traffic rerouting, commute learning)
- Implicit location inference from device/IP signals for maps requests
- Full-document fidelity guarantees for every Google-native/binary file type
