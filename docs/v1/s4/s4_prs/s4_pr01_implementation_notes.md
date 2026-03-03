# s4 pr-01 implementation notes

## scope delivered

- google connector lifecycle endpoints (`start`, `reconnect`, `callback`, `status`, `events`, `disconnect`)
- oauth authorization-code + pkce with one-time state handles, callback replay protection, and fail-closed validation
- durable connector persistence (`google_connectors`, `google_oauth_states`, `google_connector_events`)
- allowlisted read capability execution for:
  - `cap.calendar.list`
  - `cap.calendar.propose_slots`
  - `cap.email.search`
  - `cap.email.read`
- deterministic typed recoverable auth/scope failures:
  - `not_connected`
  - `consent_required`
  - `scope_missing`
  - `token_expired`
  - `access_revoked`

## hardening outcomes

### token cryptography

- token material encryption migrated from custom symmetric encoding to `AESGCM` (`cryptography`).
- ciphertext uses explicit versioned envelope format:
  - `aeadv1:<key_version>:<nonce_b64url>:<ciphertext_plus_tag_b64url>`
- key rotation support added via runtime keyring config:
  - `ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION`
  - `ARIEL_CONNECTOR_ENCRYPTION_KEYS`
- compatibility paths preserved for:
  - existing pre-hardening legacy ciphertext
  - single-secret version relabeling during staged rollouts

### provider data plane

- default google workspace provider now performs bounded live api reads:
  - calendar events (`calendar/v3/calendars/primary/events`)
  - freebusy intersection (`calendar/v3/freeBusy`)
  - gmail message search/read (`gmail/v1/users/me/messages`)
- upstream behavior hardening:
  - bounded retries on transient status codes (`429`, `500`, `502`, `503`, `504`)
  - explicit timeout/network failure handling
  - auth/scope-friendly error classification

### review-driven fixes

- tightened calendar capability validation to reject inverted time windows (`window_end <= window_start`) for:
  - `cap.calendar.list`
  - `cap.calendar.propose_slots`
- corrected connector token refresh path to persist current `encryption_key_version` after re-encryption.
- switched connector token decrypt operations to use persisted connector key version, avoiding stale-version drift risks.
- hardened AEAD decrypt parser to reject malformed envelope input and invalid nonce lengths explicitly.

## verification

- targeted hardening tests:
  - `.venv/bin/python -m pytest tests/unit/test_google_connector_hardening.py`
- acceptance coverage:
  - `.venv/bin/python -m pytest tests/integration/test_s4_pr01_acceptance.py`
- full gates:
  - `make verify`
  - `make e2e`
- manual cli verification executed against a real fastapi app + postgres testcontainer:
  - `health -> start -> callback -> status -> message(read flow) -> disconnect`
