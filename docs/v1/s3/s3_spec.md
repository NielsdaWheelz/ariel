# Slice 3: Lightweight Read Capabilities — Spec

## Goal

Prove external read integrations through Ariel's safe capability framework.

## Acceptance Criteria

### factual questions return grounded answers with inspectable sources
- **given**: an active session and a user factual query that requires external retrieval
- **when**: Ariel completes retrieval and response synthesis for that turn
- **then**: Ariel returns a direct answer grounded in retrieved evidence, includes user-visible source references in the response flow, and preserves inspectable provenance artifacts for each cited source

### unsupported factual claims are not emitted without evidence
- **given**: a factual query where retrieval returns insufficient, conflicting, or no usable evidence
- **when**: Ariel cannot ground a claim to cited sources
- **then**: Ariel does not present an uncited factual claim as true, and instead returns explicit uncertainty plus a concrete next step to recover

### weather queries return location-aware forecasts
- **given**: a user weather request with an explicit location, or a previously configured default location
- **when**: Ariel processes the turn through the weather read capability
- **then**: Ariel returns a forecast bound to the resolved location and timeframe, with clear forecast/observation timestamps and source reference(s)

### missing weather location fails with clarification, not guesswork
- **given**: a weather request without a resolvable location context
- **when**: Ariel cannot resolve location from explicit input or configured default
- **then**: Ariel asks a location clarification instead of inferring location implicitly

### topic news queries return relevant recent results
- **given**: a user request for news on a topic
- **when**: Ariel executes news retrieval for that topic
- **then**: Ariel returns relevant recent items with publication timestamps and source references, and avoids presenting stale items as current without disclosure

### web/news retrieval is provider-independent and read-policy authorized
- **given**: a factual or news request that needs external search
- **when**: Ariel executes retrieval
- **then**: retrieval runs through Ariel read capabilities (`cap.search.web`/`cap.search.news`) under read-impact policy without approval, not through model-provider-locked built-in search paths

### upstream failures are explicit and recoverable
- **given**: external retrieval APIs fail, time out, rate-limit, or return unusable data
- **when**: Ariel cannot complete retrieval normally
- **then**: Ariel returns a clear user-visible recovery path (for example retry timing, narrower query, or missing location fix), records the failure in the action lifecycle, and labels any partial result as partial

## Key Decisions

**Retrieval uses a retrieve-then-synthesize turn loop**: Slice 3 introduces a real read orchestration path where capability outputs are used to produce the final assistant answer with citations, rather than exposing raw tool output appendices as the terminal user answer.

**Provenance is first-class, not text-only**: Each retrieved source is represented as a durable provenance artifact with stable identity and citation metadata (at minimum source title, URL/source handle, and retrieval/publication timing), and user-visible references point to these artifacts.

**Provider independence is an architectural boundary**: Web/news external knowledge retrieval is capability-mediated and provider-agnostic. Brave-backed search is the MVP default backend, but Ariel’s orchestration contract remains independent of any model vendor’s native search feature set.

**No-approval reads still enforce strict runtime controls**: `cap.search.web`, `cap.search.news`, and `cap.weather.forecast` run inline as `read` actions, but still require schema validation, timeout/output bounds, redaction, auditable lifecycle events, and explicit least-privilege egress destination policy.

**Weather location resolution is deterministic and privacy-safe**: Location resolution order is explicit user location first, configured default second, clarification otherwise. Implicit IP/device geolocation inference is excluded from MVP behavior.

**External factual claims are citation-gated**: Ariel does not emit externally grounded factual assertions unless they are backed by user-visible citation references linked to provenance artifacts; insufficient evidence must produce uncertainty rather than confident unsupported claims.

**Recency and attribution are part of answer quality**: News and factual responses must carry enough timing and attribution context for the user to judge freshness and trust, and Ariel must disclose uncertainty when retrieval evidence is conflicting or insufficient.

## Out of Scope

- URL extraction, full-page parsing, and long-form URL summarization workflows (-> Slice 9)
- Google Workspace read/write domain flows (calendar/email/drive/maps) (-> Slices 4 and 8)
- Nexus notes integration behavior (-> Slice 5) and durable memory/session-rotation behavior (-> Slice 7)
- Proactive scheduled retrieval/notification execution (-> Slice 12)
- Cross-provider reliability/failover guarantees beyond preserving capability-mediated retrieval architecture (-> Slice 13)
- Auto-personalized ranking or implicit location inference from device/IP signals
