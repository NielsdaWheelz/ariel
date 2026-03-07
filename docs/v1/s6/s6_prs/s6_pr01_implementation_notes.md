# s6 pr-01 implementation notes

## scope delivered

- added drive capability contracts in the registry:
  - `cap.drive.search` (`read`, `allow_inline`)
  - `cap.drive.read` (`read`, `allow_inline`)
  - `cap.drive.share` (`external_send`, `requires_approval`)
- added least-privilege drive scope mapping for reconnect intent:
  - search: `https://www.googleapis.com/auth/drive.metadata.readonly`
  - read: `https://www.googleapis.com/auth/drive.readonly`
  - share: `https://www.googleapis.com/auth/drive`
- implemented drive provider operations in `DefaultGoogleWorkspaceProvider`:
  - drive search: retrieval-style result set with disambiguating metadata snippets
  - drive read: bounded excerpt path with typed outcomes (`unsupported`, `too_large`, `unavailable`)
  - drive share: approval-safe permission grant projection
- extended google runtime dispatch to execute drive capabilities and emit typed provider failures.
- extended google retrieval synthesis to render drive-specific recovery messaging and retrieval prefix
  (`drive results:`).
- delivered acceptance coverage for search/read/share execution policy, reconnect scope requests,
  approval exactness/exactly-once, typed auth failures, typed provider failures, and typed read outcomes.

## key hardening decisions

### scope vs provider-forbidden classification

- hardened 403 handling in google provider transport:
  - only explicit scope-failure signals map to `insufficient_permissions`.
  - provider acl/forbidden paths map to provider failure classes (not false `scope_missing`).

### shared-drive compatibility by default

- drive search/read/share requests now set `supportsAllDrives=true` where relevant.
- drive search adds `includeItemsFromAllDrives=true` so shared-drive candidates are discoverable.

### drive search safety and result hygiene

- drive query construction escapes user text and excludes trashed files:
  - `(name contains '<query>' or fullText contains '<query>') and trashed = false`

### bounded-output contract shape stability

- typed drive read outcomes now include `truncated=false` explicitly to preserve stable output shape
  alongside `content_excerpt=""`.

## files changed

- `src/ariel/capability_registry.py`
- `src/ariel/google_connector.py`
- `src/ariel/action_runtime.py`
- `tests/integration/test_s6_pr01_acceptance.py`
- `tests/unit/test_google_connector_hardening.py`
- `README.md`
- `docs/v1/s6/s6_prs/s6_pr01_implementation_notes.md`

## verification

- targeted drive acceptance:
  - `.venv/bin/python -m pytest tests/integration/test_s6_pr01_acceptance.py`
- targeted hardening unit + acceptance:
  - `.venv/bin/python -m pytest tests/unit/test_google_connector_hardening.py tests/integration/test_s6_pr01_acceptance.py`
- full verification gates:
  - `make lint`
  - `make typecheck`
  - `make verify`
- manual cli verification:
  - exercised provider-level drive search query construction/escaping and shared-drive params via
    unit-level request capture.
  - exercised drive read boundary behavior at `131072` bytes (allowed) and `131073` bytes
    (typed `too_large` outcome).
  - exercised 403 scope-vs-acl forbidden classification paths (`insufficient_permissions` vs
    `google_forbidden`).
