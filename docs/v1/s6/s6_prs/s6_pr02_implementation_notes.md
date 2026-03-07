# s6 pr-02 implementation notes

## scope delivered

- added maps capability contracts in the registry:
  - `cap.maps.directions` (`read`, `allow_inline`)
  - `cap.maps.search_places` (`read`, `allow_inline`)
- added strict maps input validation with deterministic clarification-ready execution failures:
  - `maps_origin_required`
  - `maps_destination_required`
  - `maps_location_context_required`
- implemented maps provider execution with server-side encrypted credential handling:
  - encrypted key env: `ARIEL_MAPS_PROVIDER_API_KEY_ENC`
  - decryption path reuses connector keyring/cipher semantics (`ARIEL_CONNECTOR_ENCRYPTION_*`)
- implemented stable typed maps failures:
  - credential/config: `provider_credentials_missing`, `provider_credentials_invalid`
  - runtime/provider: `provider_timeout`, `provider_network_failure`, `provider_rate_limited`,
    `provider_upstream_failure`, `provider_permission_denied`, `provider_request_rejected`,
    `provider_invalid_payload`, `provider_unreachable`
- wired explicit egress intent declarations and destination allowlists for both maps capabilities.
- extended retrieval synthesis with maps-specific recovery messaging and uncertainty handling while
  preserving citation/provenance contracts.
- added acceptance coverage for policy invariants, clarifications, typed failures, egress fail-closed,
  mixed-retrieval citations, and google-connector isolation.

## key hardening decisions

### encrypted-at-rest credential handling reused existing keyring semantics

- maps credentials are decrypted with the same key-version/keyring cipher used for connector secrets,
  avoiding a parallel cryptography path and keeping rotation semantics consistent.

### deterministic clarification over implicit geolocation

- maps validators accept optional route/location fields so runtime can emit explicit clarification
  failures instead of schema-denied ambiguity.
- assistant recovery copy explicitly states that device/ip geolocation is not inferred.

### fail-closed egress semantics kept explicit

- maps capabilities declare egress intent and destinations ahead of execution.
- non-allowlisted destinations are blocked before execute and surfaced as typed runtime failures with
  operator guidance.

## files changed

- `src/ariel/capability_registry.py`
- `src/ariel/action_runtime.py`
- `tests/integration/test_s6_pr02_acceptance.py`
- `README.md`
- `.env.example`
- `docs/v1/s6/s6_prs/s6_pr02_implementation_notes.md`

## verification

- targeted maps acceptance:
  - `.venv/bin/python -m pytest tests/integration/test_s6_pr02_acceptance.py`
- full integration regression:
  - `make test`
- static/type/full gates:
  - `make lint`
  - `make typecheck`
  - `make verify`
- manual cli verification:
  - exercised directions clarification (`maps_origin_required`) and confirmed explicit non-geolocation copy.
  - exercised maps egress fail-closed branch and confirmed pre-execution deny + operator guidance.
  - exercised successful maps retrieval response normalization with citation/source projection.
