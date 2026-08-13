# llm-tools consumer hard cut

## Summary

Replaced Ariel's `web-search-tool` dependency and imports with the public
`llm_tools` facade from `llm-tools==0.1.0`, pinned to
`667e5121268189d6fe1202c244d5ce64e8b096d1`. The lockfile, generated package
metadata, runtime imports, test fixtures, and README now use the new identity.

## Decisions

- Ariel continues to own capability policy, egress preflight, and the
  `search_results_v1` mapping; `llm-tools` owns the Brave provider binding.
- All constructed `WebSearchResponse` fixtures now set the required
  `attempts` field.
- `tests/test_llm_tools_contract.py` verifies the installed distribution's
  version and `direct_url.json` Git SHA, public-facade imports, and the absence
  of the legacy package/module.
- The focused provider test drives the real public `BraveSearchProvider`
  through Ariel with an `httpx.MockTransport`; it verifies the request and
  preserved mapped output without a live network call.

## Verification

- `uv sync --locked --extra dev` reconciled the checkout environment. It
  confirmed `llm-tools==0.1.0` at the requested Git SHA and removed the stale
  `web-search-tool` module/distribution.
- `uv lock --check` passed.
- `make lint format-check typecheck` passed (Ruff, formatting, Mypy).
- `pytest -vv tests/test_llm_tools_contract.py tests/unit/test_capability_registry_search.py::test_search_web_uses_llm_tools_public_brave_provider tests/integration/test_search_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations`
  passed: 3 passed in 9.89s.
- A clean post-sync `make verify` passed lint, format, and Mypy. Its pytest
  phase reached 153 passing tests before reproducing the unrelated existing
  failure in `test_email_state_inspection_endpoints_return_serialized_records`:
  the fixture sets `undo_expires_at` to June 2026, which is already expired on
  2026-08-13, while the assertion still expects `undo_available is True`. The
  long diagnostic run was then interrupted rather than spending another hour
  enumerating unrelated tests; it is not claimed as a green full-suite result.

## Risks

- The full-suite result is not green because of the unrelated, time-expired
  email Undo assertion above. The llm-tools-owned contract, public-provider
  boundary, search acceptance, formatting, lint, and type checks are green.
