# s4 pr-03 implementation notes

## scope delivered

- closed connector-readiness semantics so runtime auth outcomes deterministically remap readiness:
  - blocking failures (`consent_required`, `scope_missing`, `access_revoked`) now drive
    `readiness=reconnect_required`.
  - transient failures (for example `token_expired`) do not remap a healthy connected connector to
    `reconnect_required` by themselves.
- enforced reconnect-required persistence for blocking failures until a successful reconnect callback
  clears the blocking condition (or disconnect occurs).
- added explicit readiness classifier helpers and centralized connector error-set/clear behavior to avoid
  inconsistent remapping across capability paths.
- extended reconnect scope resolution for slot-planning remediation:
  - `cap.calendar.propose_slots` reconnect intent now requests attendee free/busy scope
    (`https://www.googleapis.com/auth/calendar.freebusy`) while preserving already granted scopes.
  - no unrelated scope escalation (for example calendar write or gmail send) is introduced.
- added acceptance regression coverage for:
  - blocking remap to reconnect-required
  - transient non-remap
  - sticky blocking state under later transient failures
  - attendee fallback before consent and attendee intersection after consent
  - reconnect request/audit payload correctness

## key hardening decisions

### explicit readiness classification contract

- readiness mapping is not inferred from free-text errors.
- mapping is governed by explicit code classification:
  - blocking: `consent_required`, `scope_missing`, `access_revoked`
  - transient: `token_expired`
- unknown/non-blocking error codes do not silently force reconnect-required.

### sticky blocking semantics

- once a blocking failure is observed, subsequent non-blocking errors cannot overwrite connector
  `last_error_code` until reconnect/disconnect.
- this prevents readiness from oscillating back to connected due to unrelated transient retries.

### attendee-consent reconnect closure

- reconnect intent for slot planning adds free/busy scope incrementally through
  `GOOGLE_RECONNECT_INTENT_EXTRA_SCOPES`.
- core capability required scopes remain least-privilege (`calendar.readonly` for
  `cap.calendar.propose_slots`), preserving explicit fallback behavior when attendee consent is absent.

## files changed

- `src/ariel/google_connector.py`
- `tests/integration/test_s4_pr03_acceptance.py`
- `README.md`
- `docs/v1/s4/s4_roadmap.md`

## verification

- targeted slice-4 acceptances:
  - `.venv/bin/python -m pytest tests/integration/test_s4_pr01_acceptance.py tests/integration/test_s4_pr02_acceptance.py tests/integration/test_s4_pr03_acceptance.py`
- pr-03 acceptance:
  - `.venv/bin/python -m pytest tests/integration/test_s4_pr03_acceptance.py`
- static + full regression gates:
  - `make lint`
  - `make typecheck`
  - `make verify`
  - `make e2e`
- manual CLI smoke:
  - exercised connector start/callback, slot-planning request before reconnect (fallback mode),
    reconnect with `cap.calendar.propose_slots`, callback completion, and slot-planning request after
    reconnect (attendee intersection mode).
