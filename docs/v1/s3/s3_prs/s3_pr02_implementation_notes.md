# s3 pr-02 implementation notes

## delivered scope

- added `cap.search.news` as a `read` capability with:
  - strict `query` input validation
  - explicit egress intent declaration
  - allowlisted destination preflight before execution
  - provider-agnostic output normalization into retrieval result contracts
- added `cap.weather.forecast` as a provider-abstracted `read` capability with:
  - strict `location?` + `timeframe` schema
  - deterministic execution contract (`location`, `timeframe`, forecast timestamp, source-backed snippets)
  - explicit egress intent declaration and destination allowlist preflight
- implemented weather provider abstraction:
  - production default adapter (`tomorrow.io`)
  - non-sla dev fallback adapter (`wttr`)
  - explicit mode select via `ARIEL_WEATHER_PROVIDER_MODE`
- implemented deterministic weather location resolution in orchestration:
  - explicit location first
  - canonical default location second
  - clarification path otherwise
  - no implicit ip/device location inference
- extended retrieval synthesis beyond web-only:
  - news-specific freshness disclosure for stale/missing/ambiguous publication timing
  - weather-specific response framing with resolved location/timeframe/forecast timestamp
  - retained citation + `assistant.sources[]` + artifact persistence guarantees
- added canonical weather default-location state in postgres:
  - new table: `weather_default_locations`
  - explicit APIs:
    - `GET /v1/weather/default-location`
    - `PUT /v1/weather/default-location`
  - optional env bootstrap (`ARIEL_WEATHER_DEFAULT_LOCATION`) is one-time only when canonical state is unset
  - user-set state is canonical and never overwritten by later env changes

## hardening decisions

- canonical weather default bootstrap/set paths are race-safe under concurrent requests:
  - nested-transaction insert with conflict recovery and reload
  - `set` path reconciles concurrent first-write races and still converges to user-owned canonical state
- weather dev fallback location path segments are url-encoded before outbound requests.
- weather capability allowlist is least-privilege by active provider mode (production host or dev host), not broad union.
- weather default resolution runs only for schema-shaped weather payloads to avoid mutating canonical state from malformed model payloads.

## config surface

- news:
  - `ARIEL_SEARCH_NEWS_API_KEY` (optional; falls back to `ARIEL_SEARCH_WEB_API_KEY`)
  - `ARIEL_SEARCH_NEWS_ENDPOINT`
  - `ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS`
- weather:
  - `ARIEL_WEATHER_PROVIDER_MODE` (`production` default, `dev_fallback` optional)
  - `ARIEL_WEATHER_PRODUCTION_ENDPOINT`
  - `ARIEL_WEATHER_PRODUCTION_API_KEY`
  - `ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS`
  - `ARIEL_WEATHER_DEV_ENDPOINT`
  - `ARIEL_WEATHER_DEV_TIMEOUT_SECONDS`
  - `ARIEL_WEATHER_DEFAULT_LOCATION` (bootstrap-only when canonical state is unset)

## verification run for this implementation

- targeted:
  - `pytest tests/integration/test_s3_pr02_acceptance.py tests/integration/test_s3_pr01_acceptance.py tests/integration/test_s2_pr08_acceptance.py`
- full:
  - `make verify`
  - `make e2e`
- manual cli verification:
  - exercised `GET/PUT /v1/weather/default-location`
  - exercised weather/news turn flows via `POST /v1/sessions/{session_id}/message` in both failure (missing credentials) and mocked-success paths
