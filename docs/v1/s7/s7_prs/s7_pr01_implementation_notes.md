# s7 pr-01 implementation notes

## scope delivered

- added `cap.web.extract` capability contract in the registry:
  - `read`, `allow_inline`, explicit egress declaration and allowlist preflight
  - deterministic input validation (`url` required, bounded length)
- implemented strict url safety preflight before provider dispatch:
  - invalid input -> `url_invalid`
  - non-http(s) scheme -> `url_scheme_unsupported`
  - unsafe destinations (private/loopback/link-local/multicast/reserved/blocked local suffixes)
    -> `url_destination_unsafe`
- implemented bounded provider execution with retry/timeout hardening:
  - bounded retry budget (`ARIEL_WEB_EXTRACT_MAX_RETRIES`, capped)
  - linear timeout backoff across attempts
  - stable typed provider/runtime failures
- implemented bounded extraction output with explicit partial disclosure:
  - block count and char limits
  - explicit `extract_outcome.status=partial` + recovery guidance when truncated
- extended retrieval synthesis for `cap.web.extract`:
  - grounded answer text with inline citations
  - synchronized `assistant.sources[]`
  - typed recovery hints for extraction/egress failures
- kept mixed-turn behavior grounded for retrieval while preserving structured lifecycle inspectability.
- added acceptance coverage for:
  - capability contract + egress fail-closed invariants
  - url safety preflight (including malformed ports)
  - transient retries + retry exhaustion
  - typed failures + actionable guidance
  - bounded partial disclosure
  - malformed provider final-url fail-closed behavior
  - public ipv6 canonicalization path
  - mixed-turn grounding integrity

## key hardening decisions

### canonical identity and url normalization are fail-closed

- canonicalization strips fragments/default ports/tracking keys while preserving stable source identity.
- malformed provider-resolved urls fail closed as `provider_invalid_payload` instead of silently
  degrading provenance.

### safety posture blocks local/private destinations deterministically

- host safety classification is centralized and applied both pre-dispatch and post-provider normalization.
- single-label and local-suffix hosts are treated as unsafe by policy.

### bounded extraction is explicit, never silent

- large/complex pages produce partial coverage signaling and recovery guidance.
- bounded output constraints preserve runtime budgets and response stability.

## config

- `ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT` (optional; default Brave extract endpoint)
- `ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS` (optional; default `10.0`)
- `ARIEL_WEB_EXTRACT_MAX_RETRIES` (optional; default `2`, max `5`)
- `ARIEL_WEB_EXTRACT_API_KEY` (optional; fallback to `ARIEL_SEARCH_WEB_API_KEY`)

## files changed

- `src/ariel/capability_registry.py`
- `src/ariel/action_runtime.py`
- `tests/integration/test_s7_pr01_acceptance.py`
- `README.md`
- `.env.example`
- `docs/v1/s7/s7_prs/s7_pr01_implementation_notes.md`

## verification

- targeted web-extract acceptance:
  - `.venv/bin/python -m pytest tests/integration/test_s7_pr01_acceptance.py`
- retrieval regression sweep:
  - `.venv/bin/python -m pytest tests/integration/test_s6_pr02_acceptance.py`
  - `.venv/bin/python -m pytest tests/integration/test_s3_pr03_acceptance.py`
- static/type gates:
  - `make lint`
  - `make typecheck`
- compile sanity:
  - `.venv/bin/python -m compileall src tests`
- manual cli verification:
  - exercised successful `cap.web.extract` turn and confirmed citation + artifact provenance contract.
  - exercised fail-closed path for malformed provider `final_url` and confirmed typed
    `provider_invalid_payload`.
