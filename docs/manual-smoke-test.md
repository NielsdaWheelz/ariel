# Manual Smoke Test

## Scope

This is the source-derived operator checklist for validating a live Ariel stack
after secret rotation, deploy, or incident recovery.

It owns only manual and smoke verification. Feature design stays in the module
docs; deployment steps stay in [production-runbook.md](production-runbook.md);
local development setup stays in [dev-environment.md](dev-environment.md).

## Standards

- Follow [cleanliness.md](cleanliness.md): no dead env vars, compatibility
  shims, fallback branches, or duplicate owners survive the smoke pass.
- Follow [codebase.md](codebase.md): every runtime env var read by source code
  is validated by `AppSettings` in
  [`src/ariel/config.py`](../src/ariel/config.py), except the local Docker DB
  helper vars parsed only by [`src/ariel/dev_db.py`](../src/ariel/dev_db.py).
- Follow [production-runbook.md](production-runbook.md): production binds the
  API to loopback, exposes only Google callback routes through Caddy, uses
  Discord as the primary ingress, and uses only the checked-in model refs in
  `src/ariel/models.py`.
- Treat every check as unproven until the command or UI action has been run
  against the current host and recorded.
- Never print secret values. For env checks, print names, presence, provider
  identity, file mode, and API status only.

## Evidence States

- `not_run`: no current evidence.
- `partial`: only an incomplete route, capability, or worker path was exercised.
- `passed`: current evidence proves the item.
- `failed`: current evidence contradicts the expected state.
- `blocked`: current live evidence cannot pass until user action, provider
  access, or host posture changes.
- `not_enabled`: the feature is intentionally unset in the active config or
  the active connector lacks the required grant.

## Env Var Inventory

Put production values in the env file used by the service manager for the host.
The canonical production target is `/etc/ariel/ariel.env`; current-host
deviations belong only in the dated evidence snapshot below.
`ARIEL_ENV_FILE` is a local/dev selector for commands that load env files
directly; do not put it in `/etc/ariel/ariel.env`.

### Local Env Selector

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_ENV_FILE` | no | Env-file selector | When set, only that env file is loaded; unset uses `.env` plus `.env.local`. |

### Core

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_DATABASE_URL` | yes | Postgres runtime | `AppSettings()` loads; `alembic current`; schema readiness probe has no issues. |
| `ARIEL_DEPLOYMENT_MODE` | no | Runtime security | `production` requires local auth, connector keyring, and public webhook base URL. |
| `ARIEL_BIND_HOST` | no | API bind | Must be loopback; `ss -ltnp` shows no public API bind. |
| `ARIEL_BIND_PORT` | no | API bind | `curl http://127.0.0.1:$port/v1/health`. |
| `ARIEL_LOCAL_AUTH_REQUIRED` | no | API auth | Production must be `true`; unauthenticated protected route returns auth error. |
| `ARIEL_LOCAL_AUTH_TOKEN` | yes | API auth | Token is 32+ URL-safe chars; authenticated protected route succeeds. |
| `ARIEL_SCHEMA_READINESS_TTL_SECONDS` | no | Health | `/v1/health` returns 503 with schema issues until readiness recovers. |

### Model And Loop

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_OPENAI_API_KEY` | yes | OpenAI provider | Required when `EMBEDDING` or another ref in `src/ariel/models.py` uses `provider="openai"`; live embedding smoke returns the configured vector dimension. |
| `ARIEL_OPENAI_BASE_URL` | no | OpenAI provider | OpenAI-compatible override is honored only for OpenAI provider refs. |
| `ARIEL_ANTHROPIC_API_KEY` | yes | Anthropic provider | Required when a ref in `src/ariel/models.py` uses `provider="anthropic"`; optional for the current default model refs. |
| `ARIEL_GOOGLE_API_KEY` | yes | Google provider | Required when `VISION` or another ref in `src/ariel/models.py` uses `provider="google"`; controlled image/PDF extraction proves it. |
| `ARIEL_OPENROUTER_API_KEY` | yes | OpenRouter provider | Required when `MAIN`, `RESEARCH`, or another ref in `src/ariel/models.py` uses `provider="openrouter"`. |
| `ARIEL_OPENROUTER_BASE_URL` | no | OpenRouter provider | OpenRouter-compatible override is honored only for OpenRouter provider refs. |
| `ARIEL_CLOUDFLARE_API_TOKEN` | yes | Cloudflare Workers AI provider | Required when any ref in `src/ariel/models.py` uses `provider="cloudflare"`. |
| `ARIEL_CLOUDFLARE_ACCOUNT_ID` | no | Cloudflare Workers AI provider | Account id encoded into the Workers AI base URL; required when using the Cloudflare provider. |
| `ARIEL_MODEL_TIMEOUT_SECONDS` | no | Model/audio calls | Model adapter calls and direct audio transcription time out within this bound; normal smoke turn completes. |
| `ARIEL_MAX_RESPONSE_TOKENS` | no | Agent loop | Overlong output is rejected with bounded failure. |
| `ARIEL_MAIN_TURN_BUDGET_SECONDS` | no | Agent loop | Budget exhaustion test completes gracefully. |
| `ARIEL_RESEARCH_RUN_BUDGET_SECONDS` | no | Research runtime | Research run timeout test or bounded manual research check. |
| `ARIEL_AGENT_LOOP_MAX_MODEL_CALLS` | no | Agent loop | Backstop exhaustion test completes gracefully. |
| `ARIEL_AGENT_LOOP_LIVE_ROUNDS` | no | Agent loop | Prompt-context tests verify live window behavior. |
| `ARIEL_RECENT_EVENTS_TOKEN_BUDGET` | no | Conversational continuity | Recent-events block render test stays under budget. |
| `ARIEL_RECENT_EVENTS_MAX_ROWS` | no | Conversational continuity | Recent-events query respects row cap. |
| `ARIEL_RECENT_EVENT_PAYLOAD_BYTE_CAP` | no | Conversational continuity | Oversize payloads compact to canonical IDs. |
| `ARIEL_AUTO_ROTATE_MAX_TURNS` | no | Session rotation | Rotation threshold acceptance test or manual forced threshold. |
| `ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS` | no | Session rotation | Rotation age acceptance test or manual forced threshold. |
| `ARIEL_APPROVAL_TTL_SECONDS` | no | Approval runtime | Pending approval expiry test rejects stale approval execution. |
| `ARIEL_APPROVAL_ACTOR_ID` | no | Approval runtime | Approval decisions record the configured default actor when omitted. |

### Memory

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_MEMORY_EMBEDDING_DIMENSIONS` | no | Memory schema | Must match the dimension of the `EMBEDDING` ref in `src/ariel/models.py`. |
| `ARIEL_MEMORY_RECALL_BUDGET_SECONDS` | no | Memory retriever | Memory recall syscall returns or fails within budget. |
| `ARIEL_MEMORY_ENCODE_BUDGET_SECONDS` | no | Memory rememberer | `memory.remember` enqueues and worker drains `memory_encode`. |
| `ARIEL_MEMORY_DREAM_BUDGET_SECONDS` | no | Memory dream | Recurring memory dream task runs within budget. |
| `ARIEL_MEMORY_DREAM_INTERVAL_SECONDS` | no | Memory dream | Recurring task re-arms at the configured interval. |

### Google Connector And Push

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_GOOGLE_OAUTH_CLIENT_ID` | no | OAuth | Google connector start returns a consent URL for this client. |
| `ARIEL_GOOGLE_OAUTH_CLIENT_SECRET` | yes | OAuth | Callback exchanges code; connector reaches `connected`. |
| `ARIEL_GOOGLE_OAUTH_REDIRECT_URI` | no | OAuth | Must match the Google console authorized redirect URI exactly. |
| `ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS` | no | OAuth | Expired state test rejects stale callback. |
| `ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS` | no | OAuth | Provider timeout is bounded. |
| `ARIEL_CONNECTOR_ENCRYPTION_SECRET` | yes | Token encryption | Production rejects dev default. |
| `ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION` | no | Token encryption | Active version exists in `ARIEL_CONNECTOR_ENCRYPTION_KEYS`. |
| `ARIEL_CONNECTOR_ENCRYPTION_KEYS` | yes | Token encryption | Existing encrypted connector tokens decrypt after restart. |
| `ARIEL_PUBLIC_WEBHOOK_BASE_URL` | no | Google push | HTTPS base URL has no path/query; Caddy routes only Google provider events and OAuth callback. |
| `ARIEL_GOOGLE_PUBSUB_TOPIC` | no | Gmail watch | Topic exists, Gmail publisher IAM binding is present, and topic/subscription/credentials are set as one group. |
| `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION` | no | Pub/Sub subscriber | Subscription exists and is paired with topic plus credentials path. |
| `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH` | no | Pub/Sub subscriber | Absolute JSON path exists, mode `0600`, and is paired with topic plus subscription; never print the JSON contents. |
| `ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS` | no | Worker sync | Recurring reconcile rows re-arm after successful runs; production posture rejects sub-minute intervals. |
| `ARIEL_SUBSCRIBER_HEARTBEAT_INTERVAL_SECONDS` | no | Pub/Sub health | `/v1/health` reports fresh subscriber heartbeat. |
| `ARIEL_SUBSCRIBER_HEARTBEAT_STALENESS_FACTOR` | no | Pub/Sub health | Health marks stale subscriber after missed heartbeat window. |

### Discord

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_DISCORD_BOT_TOKEN` | yes | Discord bot | `GET /users/@me` succeeds; gateway connects. |
| `ARIEL_DISCORD_GUILD_ID` | no | Discord bot | Bot can read the home guild. |
| `ARIEL_DISCORD_CHANNEL_ID` | no | Notifications | Bot can read the default channel and deliver a controlled notification. |
| `ARIEL_DISCORD_USER_ID` | no | Owner ingress | Owner DM is accepted; non-owner DM is ignored. |
| `ARIEL_DISCORD_ARIEL_BASE_URL` | no | Discord-to-API client | Bot reaches the loopback API health route. |
| `ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS` | no | Notification client | Discord send failures time out within this bound. |

The Discord application/client ID is a provider-portal value for invite URL
construction. It is not an Ariel environment variable and must not be added to
runtime env files.

### Agency

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_AGENCY_SOCKET_PATH` | no | Agency client | Socket exists and `agency.status` can read a smoke job. |
| `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` | no | Agency policy | Contains only approved absolute repo roots. |
| `ARIEL_AGENCY_DEFAULT_BASE_BRANCH` | no | Agency jobs | Smoke job targets the expected base branch. |
| `ARIEL_AGENCY_DEFAULT_RUNNER` | no | Agency jobs | Smoke job uses the expected runner. |
| `ARIEL_AGENCY_TIMEOUT_SECONDS` | no | Agency client | Socket calls time out within this bound. |
| `ARIEL_AGENCY_EVENT_SECRET` | yes | Agency webhook | When unset, event ingress returns disabled; when set, signed event is accepted. |
| `ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS` | no | Agency webhook | Stale signed event rejected. |

### Provider Capabilities

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_SEARCH_WEB_API_KEY` | yes | Brave-backed `search.web` | Web smoke authenticates with the Brave key. |
| `ARIEL_SEARCH_BRAVE_BASE_URL` | no | Search provider | Clean HTTPS provider URL; production pins the Brave API host. |
| `ARIEL_SEARCH_WEB_TIMEOUT_SECONDS` | no | Search provider | Timeout path is bounded. |
| `ARIEL_JINA_API_KEY` | yes (for `web.extract`) | Jina Reader (https://r.jina.ai) | `cap.web.extract` is bound iff this key is set; extraction smoke succeeds with a real URL. |
| `ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS` | no | Jina Reader | Timeout path is bounded. |
| `ARIEL_WEB_EXTRACT_MAX_RETRIES` | no | Jina Reader | Value is between 0 and 5. |
| `ARIEL_MAPS_API_KEY` | yes | Maps | Directions and place search smokes succeed; key is API/IP restricted. |
| `ARIEL_MAPS_TIMEOUT_SECONDS` | no | Maps | Timeout path is bounded. |
| `ARIEL_WEATHER_PROVIDER_MODE` | no | Weather | `production` or `dev`; production deployment rejects dev mode. |
| `ARIEL_WEATHER_PRODUCTION_ENDPOINT` | no | Weather | Clean HTTPS provider URL; production pins the Tomorrow.io API host. |
| `ARIEL_WEATHER_PRODUCTION_API_KEY` | yes | Weather | Forecast smoke succeeds in production mode. |
| `ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS` | no | Weather | Timeout path is bounded. |
| `ARIEL_WEATHER_DEV_ENDPOINT` | no | Weather dev | Clean HTTPS provider URL when dev mode is used; default is `wttr.in`. |
| `ARIEL_WEATHER_DEV_TIMEOUT_SECONDS` | no | Weather dev | Timeout path is bounded. |
| `ARIEL_WEATHER_DEFAULT_LOCATION` | no | Weather state | Seeds canonical state only when unset. |

### Attachments And Worker

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_ATTACHMENT_BLOB_STORE_PATH` | no | Attachment store | Directory is writable by the API service user. |
| `ARIEL_ATTACHMENT_MAX_BYTES` | no | Attachment ingress | Oversized attachment is rejected. |
| `ARIEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS` | no | Attachment fetch | Fetch timeout path is bounded. |
| `ARIEL_ATTACHMENT_HANDLE_TTL_SECONDS` | no | Attachment refs | Expired handle is rejected. |
| `ARIEL_ATTACHMENT_SCANNER_MODE` | no | Attachment extraction | `fail_closed` blocks newly fetched extraction until scanner support exists. |
| `ARIEL_ATTACHMENT_OPENAI_AUDIO_MODEL` | no | Audio extraction | Audio extraction uses intended model when enabled. |
| `ARIEL_WORKER_POLL_SECONDS` | no | Worker loop | Worker idles and drains due rows at this cadence. |

### Local DB Helper Only

These are parsed only by `src/ariel/dev_db.py` and must not be treated as app
runtime settings:

- `ARIEL_DB_CONTAINER_NAME`
- `ARIEL_DB_DOCKER_IMAGE`
- `ARIEL_DB_VOLUME_NAME`

### Env Var Evidence Ledger

This ledger records current-host env evidence without printing secret values.
`partial` means the key is tracked by validators and the redacted env load, but
the owning feature still needs its own live smoke row.

| Env var | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_AGENCY_DEFAULT_BASE_BRANCH` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_AGENCY_DEFAULT_RUNNER` | `passed` | `failed` | Validator/inventory guards track this key; current redacted env load is present, but canonical production posture expects `codex`. |
| `ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_AGENCY_EVENT_SECRET` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; signed-event route smoke proves the ingress path. |
| `ARIEL_AGENCY_SOCKET_PATH` | `passed` | `blocked` | Validator/inventory guards track this key; redacted current env load: present, but canonical `/var/lib/agency/agencyd.sock` production posture is absent. |
| `ARIEL_AGENCY_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_AGENT_LOOP_LIVE_ROUNDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_AGENT_LOOP_MAX_MODEL_CALLS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_RECENT_EVENT_PAYLOAD_BYTE_CAP` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_RECENT_EVENTS_MAX_ROWS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_RECENT_EVENTS_TOKEN_BUDGET` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_ANTHROPIC_API_KEY` | `passed` | `not_enabled` | Validator/inventory guards track this supported provider key; current default model refs do not use Anthropic. |
| `ARIEL_APPROVAL_ACTOR_ID` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_APPROVAL_TTL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ATTACHMENT_BLOB_STORE_PATH` | `passed` | `failed` | Validator/inventory guards track this key; current redacted env load is present, but canonical production posture expects `/var/lib/ariel/attachment-blobs`. |
| `ARIEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ATTACHMENT_HANDLE_TTL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ATTACHMENT_MAX_BYTES` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ATTACHMENT_OPENAI_AUDIO_MODEL` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ATTACHMENT_SCANNER_MODE` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; attachment capability and product rows own fail-closed runtime smoke state. |
| `ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_AUTO_ROTATE_MAX_TURNS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_BIND_HOST` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; production posture requires loopback only. |
| `ARIEL_BIND_PORT` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_CLOUDFLARE_ACCOUNT_ID` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, and no current model ref uses Cloudflare. |
| `ARIEL_CLOUDFLARE_API_TOKEN` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, and no current model ref uses Cloudflare. |
| `ARIEL_CONNECTOR_ENCRYPTION_KEYS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_CONNECTOR_ENCRYPTION_SECRET` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; production posture rejects the dev fallback. |
| `ARIEL_DATABASE_URL` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; DB route smokes and migrations own full schema proof. |
| `ARIEL_DB_CONTAINER_NAME` | `passed` | `partial` | Local-only `dev_db` helper guard tracks this key; redacted current local env load: present; production `/etc/ariel/ariel.env` audit rejects it as unknown. |
| `ARIEL_DB_DOCKER_IMAGE` | `passed` | `partial` | Local-only `dev_db` helper guard tracks this key; redacted current local env load: present; production `/etc/ariel/ariel.env` audit rejects it as unknown. |
| `ARIEL_DB_VOLUME_NAME` | `passed` | `partial` | Local-only `dev_db` helper guard tracks this key; redacted current local env load: present; production `/etc/ariel/ariel.env` audit rejects it as unknown. |
| `ARIEL_DEPLOYMENT_MODE` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_DISCORD_ARIEL_BASE_URL` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; current value reaches loopback API. |
| `ARIEL_DISCORD_BOT_TOKEN` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; gateway connectivity was proven without printing the token. |
| `ARIEL_DISCORD_CHANNEL_ID` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_DISCORD_GUILD_ID` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_DISCORD_USER_ID` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_ENV_FILE` | `passed` | `partial` | Env-file selector guard tracks this key; redacted current env load: unset, so `.env` plus `.env.local` are used. |
| `ARIEL_GOOGLE_API_KEY` | `passed` | `passed` | Validator/inventory guards track this current `VISION` provider key; redacted current env load is present after key rotation, and direct Gemini text plus tiny binary-image smokes passed. |
| `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; Pub/Sub key file path was checked without printing the JSON. |
| `ARIEL_GOOGLE_OAUTH_CLIENT_ID` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_GOOGLE_OAUTH_CLIENT_SECRET` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_GOOGLE_OAUTH_REDIRECT_URI` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; Google reconnect flow uses this exact callback. |
| `ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; subscriber heartbeat proves the configured path is active. |
| `ARIEL_GOOGLE_PUBSUB_TOPIC` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_JINA_API_KEY` | `passed` | `passed` | Validator/inventory guards track this key; redacted current env load: present; live `web.extract` Jina Reader smoke passed. |
| `ARIEL_LOCAL_AUTH_REQUIRED` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; protected-route auth smokes prove enforcement. |
| `ARIEL_LOCAL_AUTH_TOKEN` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; authenticated loopback smokes use it without printing it. |
| `ARIEL_MAIN_TURN_BUDGET_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MAPS_API_KEY` | `passed` | `passed` | Validator/inventory guards track this key; redacted current env load: present; live Maps directions and places smokes passed. |
| `ARIEL_MAPS_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MAX_RESPONSE_TOKENS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_MEMORY_DREAM_BUDGET_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MEMORY_DREAM_INTERVAL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MEMORY_EMBEDDING_DIMENSIONS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; schema dimension must match `EMBEDDING`. |
| `ARIEL_MEMORY_ENCODE_BUDGET_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MEMORY_RECALL_BUDGET_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_MODEL_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_OPENAI_API_KEY` | `passed` | `partial` | Validator/inventory guards track this current `EMBEDDING` provider key; redacted current env load: present. |
| `ARIEL_OPENAI_BASE_URL` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, so the default OpenAI endpoint is used. |
| `ARIEL_OPENROUTER_API_KEY` | `passed` | `passed` | Validator/inventory guards track this current `MAIN` and `RESEARCH` provider key; redacted current env load is present, and app-like direct adapter smokes passed for both refs. |
| `ARIEL_OPENROUTER_BASE_URL` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, so the default OpenRouter endpoint is used. |
| `ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies; production posture rejects values below 60 seconds. |
| `ARIEL_PUBLIC_WEBHOOK_BASE_URL` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; public Caddy/Google callback smokes cover reachability. |
| `ARIEL_RESEARCH_RUN_BUDGET_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_SCHEMA_READINESS_TTL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: unset, so the code default applies. |
| `ARIEL_SEARCH_BRAVE_BASE_URL` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present; live `search.web` smoke uses Brave. |
| `ARIEL_SEARCH_WEB_API_KEY` | `passed` | `passed` | Validator/inventory guards track this key; redacted current env load: present; live Brave `search.web` smoke passed. |
| `ARIEL_SEARCH_WEB_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_SUBSCRIBER_HEARTBEAT_INTERVAL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_SUBSCRIBER_HEARTBEAT_STALENESS_FACTOR` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WEATHER_DEFAULT_LOCATION` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, so no bootstrap default location is seeded. |
| `ARIEL_WEATHER_DEV_ENDPOINT` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, and production weather mode is active. |
| `ARIEL_WEATHER_DEV_TIMEOUT_SECONDS` | `passed` | `not_enabled` | Validator/inventory guards track this key; redacted current env load: unset, and production weather mode is active. |
| `ARIEL_WEATHER_PRODUCTION_API_KEY` | `passed` | `passed` | Validator/inventory guards track this key; redacted current env load: present; live Tomorrow.io weather smoke passed. |
| `ARIEL_WEATHER_PRODUCTION_ENDPOINT` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WEATHER_PROVIDER_MODE` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WEB_EXTRACT_MAX_RETRIES` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |
| `ARIEL_WORKER_POLL_SECONDS` | `passed` | `partial` | Validator/inventory guards track this key; redacted current env load: present. |

## HTTP And User Action Inventory

All `/v1/*` routes require local bearer auth when
`ARIEL_LOCAL_AUTH_REQUIRED=true` except `GET /v1/health`, the Google OAuth
callback, the Google provider event webhook, and signed Agency event ingress.
Those public ingress routes still enforce their own route-specific validation.

### Public Local-Auth Bypass Routes

| Route |
| --- |
| `GET /v1/health` |
| `GET /v1/connectors/google/callback` |
| `POST /v1/providers/google/events` |
| `POST /v1/agency/events` |

### Public Caddy Ingress Routes

Caddy exposes only the Google Calendar provider webhook and OAuth callback to the public
internet. `GET /v1/health` and `POST /v1/agency/events` bypass local auth inside
the loopback app, but Caddy must not forward them publicly.

| Route |
| --- |
| `GET /v1/connectors/google/callback` |
| `POST /v1/providers/google/events` |

### Generated Developer Docs Routes

FastAPI's generated `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, and
`/redoc` routes are development-only. Production `create_app()` disables them,
so they are excluded from the product route inventory and must not be exposed by
Caddy.

### HTTP Route Inventory

| Surface | Action | Smoke evidence |
| --- | --- | --- |
| `GET /` | Root liveness | Authenticated loopback returns a small root response; Caddy public root returns 404. |
| `GET /v1/health` | Health | Returns `ok` plus subscriber heartbeat when configured; returns 503 with schema issues when migrations are not ready. |
| `POST /v1/agency/events` | Agency signed event ingress | Disabled without `ARIEL_AGENCY_EVENT_SECRET`; missing or bad signature rejected when enabled; signed event accepted when secret is set. |
| `POST /v1/sessions` | Create or get session | Returns a session id and durable session row. |
| `GET /v1/sessions/active` | Active session | Returns current session. |
| `POST /v1/sessions/rotate` | Manual rotation | Creates a new active session and rotation record. |
| `GET /v1/sessions/rotations` | Rotation history | Lists recent rotations. |
| `POST /v1/sessions/{session_id}/message` | Direct user message | Enqueues a `user_message` task; worker completion creates the turn and optional Discord reply. |
| `GET /v1/sessions/{session_id}/events` | Timeline | Shows turns, events, approvals, and messages. |
| `GET /v1/weather/default-location` | Read weather location | Returns canonical state. |
| `PUT /v1/weather/default-location` | Set weather location | Updates canonical state and rejects invalid input. |
| `GET /v1/connectors/google` | Google connector status | Shows connected identity, readiness, granted scopes, token state, and last connector error. |
| `GET /v1/connectors/google/events` | Google connector event list | Lists stored connector events. |
| `POST /v1/connectors/google/start` | Start Google connect | Returns OAuth consent URL. |
| `POST /v1/connectors/google/reconnect` | Reconnect Google | Returns OAuth consent URL for replacement credentials and optional capability-intent scope expansion. |
| `GET /v1/connectors/google/callback` | Google OAuth callback | Exchanges code, stores encrypted tokens, registers watches. |
| `DELETE /v1/connectors/google` | Disconnect Google | Revokes local connector state and watches. |
| `POST /v1/captures/record` | Capture ingress | Stores a durable capture without enqueuing agent work. |
| `POST /v1/approvals` | Approval decision | Approves/rejects a pending action once; replay is safe but returns not-pending after resolution. |
| `POST /v1/providers/google/events` | Calendar webhook | Authenticates watch token and enqueues provider ingest. |
| `GET /v1/connectors/{provider}/sync-cursors` | Sync cursor read | Lists provider cursor state. |
| `POST /v1/connectors/{provider}/sync` | Force provider sync | Enqueues Gmail, Calendar, or Drive sync and wakes on changes. |
| `GET /v1/provider-events` | Provider event list | Lists raw stored provider event rows. |
| `GET /v1/sync-runs` | Sync run list | Lists recent sync runs. |
| `GET /v1/email/actions` | Email mailbox mutation action list | Lists durable email mailbox mutations for required `provider_account_id`; draft/send receipts are not part of this route. |
| `GET /v1/email/actions/{email_action_id}` | Email mailbox mutation action detail | Shows one email mailbox mutation and receipts for required `provider_account_id`; draft/send receipts are not part of this route. |
| `GET /v1/discord-messages` | Discord message list | Shows accepted Discord ingress rows. |
| `GET /v1/discord-messages/{discord_message_id}/events` | Discord timeline | Shows events for one Discord message. |
| `GET /v1/jobs` | Job list | Lists Agency jobs. |
| `GET /v1/jobs/{job_id}` | Job detail | Shows one Agency job. |
| `GET /v1/jobs/{job_id}/events` | Job events | Shows Agency job timeline. |
| `GET /v1/artifacts/{artifact_id}` | Artifact read | Returns stored artifact metadata. |
| `GET /v1/memory/log` | Memory log | Lists memory substrate events. |
| `GET /v1/memory/notes` | Memory notes | Lists operator-visible notes. |

### HTTP Route Evidence Ledger

This ledger separates route-contract evidence from current-host smoke evidence.
Contract `passed` means the route behavior is proven by either route-specific
fixture evidence or current-host route evidence. Current-host state records only
this host's smoke pass. Keep destructive or provider-mutating live drills
`not_run` until they are part of an intentional operator-approved drill.

| Route | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `GET /` | `passed` | `passed` | Live authenticated loopback root returned a bounded root response; public Caddy root returned 404. |
| `GET /v1/health` | `passed` | `passed` | Live authenticated loopback health returned `ok: true` with fresh subscriber heartbeat. |
| `POST /v1/agency/events` | `passed` | `passed` | Live unsigned event was rejected; signed manual event was accepted and worker-processed into a job event. Fixture anchors: `tests/integration/test_api_auth.py::test_agency_event_ingress_rejects_missing_secret`, `tests/integration/test_api_auth.py::test_agency_event_ingress_rejects_bad_signatures`, and `tests/integration/test_discord_primary_durable_workflows_acceptance.py::test_agency_event_ingress_is_signed_idempotent_and_rejects_conflicts`. |
| `POST /v1/sessions` | `passed` | `passed` | Live smoke returned the active session id; fixture anchor: `tests/integration/test_turn_lifecycle_acceptance.py::test_create_session_endpoint_reuses_single_active_session`. |
| `GET /v1/sessions/active` | `passed` | `passed` | Live smoke returned the current active session. |
| `POST /v1/sessions/rotate` | `passed` | `not_run` | Fixture-backed manual rotation creates a new active session and idempotent replay returns the same rotation; destructive live rotation remains an operator-only drill. Fixture anchor: `tests/integration/test_session_management_acceptance.py::test_manual_rotation_endpoint_creates_new_active_session_and_is_idempotent`. |
| `GET /v1/sessions/rotations` | `passed` | `passed` | Live smoke listed recent rotation records. |
| `POST /v1/sessions/{session_id}/message` | `passed` | `partial` | Historical live route accepted and drained a `user_message` task, but that turn did not complete under the previous missing main-model key; rerun a direct-message smoke against the current OpenRouter `MAIN` ref before marking this passed. Fixture-backed route enqueues a user-message task, rejects idempotency conflicts, drains the task, and replays the completed turn without a second model call. Fixture anchor: `tests/integration/test_session_management_acceptance.py::test_message_idempotency_key_replays_same_task_id`. |
| `GET /v1/sessions/{session_id}/events` | `passed` | `passed` | Live smoke read the active session timeline. |
| `GET /v1/weather/default-location` | `passed` | `passed` | Live smoke read canonical weather state. |
| `PUT /v1/weather/default-location` | `passed` | `passed` | Live smoke rejected blank input, round-tripped a controlled default location through the API, and restored the prior unset state; fixture anchor: `tests/integration/test_search_weather_acceptance.py::test_weather_default_location_is_canonical_state_with_env_bootstrap_once_only`. |
| `GET /v1/connectors/google` | `passed` | `passed` | Live smoke showed connected identity, readiness, token state, granted scopes, and no last connector error. |
| `GET /v1/connectors/google/events` | `passed` | `passed` | Live smoke listed connector events. |
| `POST /v1/connectors/google/start` | `passed` | `not_run` | Fixture-backed connect flow creates OAuth state before callback; live `/start` is only for first-time connect or planned credential replacement. Fixture anchor: `tests/integration/test_google_provider_ingestion.py::test_connect_registers_watches_and_calendar_push_accepts_persisted_token`. |
| `POST /v1/connectors/google/reconnect` | `passed` | `partial` | Live recovery flow returned a reconnect OAuth consent URL; credential restoration belongs to the callback route after consent. Fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable`. |
| `GET /v1/connectors/google/callback` | `passed` | `partial` | Fixture-backed callback consumes OAuth state, stores the connected account, registers watches, and rejects registration failures with typed connector errors. Live public invalid callback rejects as expected. Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_connect_registers_watches_and_calendar_push_accepts_persisted_token` and `tests/integration/test_google_provider_ingestion.py::test_connect_watch_registration_failure_fails_callback`. |
| `DELETE /v1/connectors/google` | `passed` | `not_run` | Fixture-backed disconnect clears connector watch channels, sync cursors, provider events, and queued one-shot provider work; destructive live disconnect remains a planned credential-replacement drill. Fixture anchor: `tests/integration/test_google_provider_ingestion.py::test_disconnect_clears_google_provider_ingestion_state`. |
| `POST /v1/captures/record` | `passed` | `passed` | Live smoke created a durable capture and did not enqueue background work. |
| `POST /v1/approvals` | `passed` | `passed` | Live rollback smoke denied a synthetic pending approval, returned no execution task, and cleanup left zero seeded rows; fixture anchor: `tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_denies_without_enqueuing_execution`. |
| `POST /v1/providers/google/events` | `passed` | `partial` | Fixture-backed Calendar webhook ingest authenticates the persisted watch channel token, stores one provider event, enqueues one exact `provider_sync_due` task after worker processing, and returns duplicate-safe acceptance; public unauthenticated live POST still fails validation as expected. Fixture anchor: `tests/integration/test_discord_primary_durable_workflows_acceptance.py::test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe`. |
| `GET /v1/connectors/{provider}/sync-cursors` | `passed` | `passed` | Live smoke listed Google sync cursor state. |
| `POST /v1/connectors/{provider}/sync` | `passed` | `not_run` | Fixture-backed force-sync route enqueues `provider_sync_due` with the selected provider/resource payload; live worker drainage still calls Google and mutates sync state. Fixture anchor: `tests/integration/test_google_provider_ingestion.py::test_connector_sync_cursor_routes_list_cursors_and_enqueue_forced_sync`. |
| `GET /v1/provider-events` | `passed` | `passed` | Live smoke listed raw provider event rows. |
| `GET /v1/sync-runs` | `passed` | `passed` | Live smoke listed recent sync runs. |
| `GET /v1/email/actions` | `passed` | `passed` | Live list-route shape smoke listed email action ids for the connected provider account when present; it does not prove a controlled mailbox mutation. |
| `GET /v1/email/actions/{email_action_id}` | `passed` | `passed` | Live controlled email action rollback smoke seeds a synthetic `cap.email.archive` receipt, reads it by id, and cleans it up; this proves the HTTP serializer, not a provider mutation. Fixture anchor: `tests/integration/test_email_decluttering_api.py::test_email_state_inspection_endpoints_return_serialized_records`. |
| `GET /v1/discord-messages` | `passed` | `passed` | Live list-route shape smoke listed accepted Discord ingress row ids; it does not prove a fresh Discord UI interaction. |
| `GET /v1/discord-messages/{discord_message_id}/events` | `passed` | `passed` | Live controlled Discord rollback smoke seeds a synthetic Discord message/event pair, reads the event route by id, and cleans it up; this proves the HTTP serializer, not a fresh Discord UI interaction. Fixture anchor: `tests/integration/test_discord_message_acceptance.py::test_no_visible_response_operation_completes_turn_without_visible_reply`. |
| `GET /v1/jobs` | `passed` | `passed` | Live list-route shape smoke listed Agency job ids; controlled detail/event proof belongs to the signed Agency event block. |
| `GET /v1/jobs/{job_id}` | `passed` | `passed` | Live controlled job rollback smoke seeds a synthetic Agency job, reads it by id, and cleans it up; this proves the HTTP serializer, not Agency daemon execution. Fixture anchor: `tests/integration/test_no_ai_ops_acceptance.py::test_jobs_endpoint_lists_recent_jobs_deterministically`. |
| `GET /v1/jobs/{job_id}/events` | `passed` | `passed` | Live controlled job rollback smoke seeds a synthetic Agency job event, reads the event route by job id, and cleans it up; this proves the HTTP serializer, not Agency daemon execution. Fixture anchor: `tests/integration/test_no_ai_ops_acceptance.py::test_job_events_endpoint_lists_job_events_deterministically`. |
| `GET /v1/artifacts/{artifact_id}` | `passed` | `passed` | Live controlled artifact rollback smoke seeds a synthetic `retrieval_provenance` artifact, reads it by id, and cleans it up; this proves the HTTP serializer, not retrieval execution. Fixture anchors: `tests/integration/test_search_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations` and `tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance`. |
| `GET /v1/memory/log` | `passed` | `passed` | Live smoke listed memory log rows. |
| `GET /v1/memory/notes` | `passed` | `passed` | Live smoke listed memory notes. |

### Google Reconnect Evidence Ledger

`POST /v1/connectors/google/reconnect` owns both baseline credential refresh and
capability-intent scope expansion. Existing grants are carried forward; invalid
intent bundles reject the whole reconnect request.

| Reconnect behavior | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| baseline reconnect | `passed` | `partial` | Live recovery flow returned OAuth consent; credential restoration is proven by callback/connector-status evidence, not by `/reconnect` alone. Fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable`. |
| single capability_intent | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_drive_capabilities_acceptance.py::test_smoke_unblock_reconnect_intents_request_expected_scope` and `tests/integration/test_google_connector_readiness_acceptance.py::test_attendee_reconnect_intent_requests_freebusy_and_restores_full_availability`. |
| comma-bundled capability_intents | `passed` | `not_run` | Fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_smoke_unblock_reconnect_accepts_comma_separated_intents_in_one_call` proves the response and started event payload preserve the normalized intent list. |
| invalid capability_intent | `passed` | `not_run` | Fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_smoke_unblock_reconnect_rejects_any_invalid_intent_in_comma_list` proves invalid bundles create no OAuth state or connector event. |
| reconnect event payload | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_connector_readiness_acceptance.py::test_attendee_reconnect_intent_requests_freebusy_and_restores_full_availability` and `tests/integration/test_google_drive_capabilities_acceptance.py::test_smoke_unblock_reconnect_accepts_comma_separated_intents_in_one_call` prove the started event records requested intents and scopes. |

### Capture Kind Evidence Ledger

`POST /v1/captures/record` accepts all capture kinds through the same route.
Discord `/capture` submits only `text`; browser/share-sheet callers own `url`
and `shared_content`.

| Capture kind | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `text` | `passed` | `passed` | Live HTTP capture smoke created a durable text capture without background work; fixture anchor: `tests/integration/test_no_ai_ops_acceptance.py::test_capture_record_creates_durable_capture_without_model`. |
| `url` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_no_ai_ops_acceptance.py::test_capture_record_idempotency_blocks_payload_conflicts`. |
| `shared_content` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_no_ai_ops_acceptance.py::test_capture_record_supports_shared_content_without_model`. |

## Discord User Actions

| Action | Expected behavior | Smoke evidence |
| --- | --- | --- |
| Discord application setup | Bot is installed with `bot` and `applications.commands`; privileged Message Content Intent is enabled in the Discord developer portal. | `/status` renders and an owner ambient message reaches Ariel with nonblank content. |
| Bot or self message | Ignored. | No API turn is created. |
| Blank owner message | Ignored. | No API turn is created. |
| Owner DM | Accepted as ambient agent input. | Bot visibly replies in the DM or intentionally finishes silently; no default-guild notification is sent for the reply. |
| Home-guild ambient message | Accepted from the configured owner in the configured guild. | Bot replies in the origin channel or thread, or intentionally finishes silently. |
| Non-owner message | Ignored in DM and guild contexts. | No API turn is created. |
| Wrong-guild message | Ignored. | No API turn is created. |
| Unsupported Discord message type | Ignored. | No API turn is created. |
| Bot mention | Mention text is stripped before submission. | API turn stores the cleaned user prompt. |
| Mention-only bot ping | Ignored after mention stripping. | No API turn is created. |
| Owner DM attachment-only no-instruction message | Accepted with attachment refs but no blind read. | API turn includes attachment refs, no raw Discord CDN URL is model-visible, and Ariel asks what to do or intentionally stays silent. |
| Home-guild attachment-only no-instruction message | Accepted with attachment refs but no blind read. | API turn includes attachment refs, no raw Discord CDN URL is model-visible, and Ariel asks what to do or intentionally stays silent. |
| Owner DM attachment read request | Accepted with an explicit content intent. | API turn includes attachment refs, no raw Discord CDN URL is model-visible, and `fail_closed` read returns a typed scan result unless a clean cached blob or extraction exists. |
| Home-guild attachment read request | Accepted with an explicit content intent. | API turn includes attachment refs, no raw Discord CDN URL is model-visible, and `fail_closed` read returns a typed scan result unless a clean cached blob or extraction exists. |
| Origin reply routing | Worker sends assistant output to the Discord origin when an origin message exists. | DM and guild-origin turns reply to their origin message/channel and do not fall back to the default channel. |
| Default notification delivery | Worker sends no-origin notifications to `ARIEL_DISCORD_CHANNEL_ID`. | A controlled no-origin notification appears in the configured default channel. |
| `/status` | Deterministic operational status. | Command returns health-ok, active session, and recent job counts. |
| `/jobs` | Deterministic job list. | Command returns recent Agency jobs. |
| `/capture` | Deterministic capture submission. | Capture row appears; capture ingress does not enqueue background work. |
| Wrong-user slash command | Rejected ephemerally. | No API mutation is attempted. |
| Wrong-guild slash command | Rejected ephemerally. | No API mutation is attempted. |
| Wrong-user approval button | Rejected ephemerally. | No approval API mutation is attempted. |
| Wrong-guild approval button | Rejected ephemerally. | No approval API mutation is attempted. |
| Non-Ariel component custom_id | Ignored silently. | No API mutation or Discord response is attempted. |
| Approval approve button | Owner approval button approves one pending action. | Action execution is queued once and button UI is removed or disabled. |
| Approval deny button | Owner denial button rejects one pending action. | Action execution is not queued and button UI is removed or disabled. |
| Approval stale/replay interaction | Stale or duplicate approval interactions do not duplicate side effects. | Replay returns not-pending or equivalent rejection without creating another action attempt. |
| Malformed approval button custom_id | Rejected ephemerally. | No approval API mutation is attempted. |

### Discord User Action Evidence Ledger

This ledger separates code/fixture coverage from current-host Discord UI
evidence. Current-host `passed` requires a real Discord owner or non-owner
interaction against the configured bot; fixture coverage alone keeps live state
`not_run` or `blocked`.

| Action | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| Discord application setup | `partial` | `not_run` | Historical bot token, guild, channel, and gateway connectivity were proven, but no fresh Discord portal or gateway smoke has been recorded after local edits; developer-portal install scopes and Message Content Intent still need UI confirmation. |
| Bot or self message | `passed` | `not_run` | Fixture-backed bot and self-authored messages are ignored without API mutation. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_ignores_unsupported_messages` and `tests/unit/test_discord_bot.py::test_on_message_ignores_self_authored_message`. |
| Blank owner message | `passed` | `not_run` | Fixture-backed blank owner messages are ignored without API mutation. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_ignores_unsupported_messages`. |
| Owner DM | `passed` | `partial` | Fixture-backed owner DM submits one Discord-originated turn; fresh live Discord owner-DM UI completion against the current OpenRouter `MAIN` ref has not been recorded. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_answers_configured_user_dm`. |
| Home-guild ambient message | `passed` | `partial` | Fixture-backed home-guild ambient messages submit turns from any channel; fresh live Discord guild-message UI completion against the current OpenRouter `MAIN` ref has not been recorded. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_answers_home_guild_message_in_any_channel`. |
| Non-owner message | `passed` | `not_run` | Fixture-backed non-owner DM and home-guild ambient messages are ignored without API mutation. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_ignores_non_owner_dm` and `tests/unit/test_discord_bot.py::test_on_message_ignores_non_owner_home_guild_ambient_message`. |
| Wrong-guild message | `passed` | `not_run` | Fixture-backed owner messages outside the home guild are ignored for direct mentions, replies, and unmentioned chatter. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_ignores_other_server_direct_mention`, `tests/unit/test_discord_bot.py::test_on_message_ignores_other_server_reply`, and `tests/unit/test_discord_bot.py::test_on_message_ignores_other_server_unmentioned_message`. |
| Unsupported Discord message type | `passed` | `not_run` | Fixture-backed unsupported Discord message types are ignored without API mutation. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_ignores_unsupported_messages`. |
| Bot mention | `passed` | `not_run` | Fixture-backed direct mentions strip the bot token before submitting the prompt. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_strips_direct_bot_mention_from_prompt`. |
| Mention-only bot ping | `passed` | `not_run` | Fixture-backed mention-only owner pings are ignored after mention stripping and do not call the API. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_message_ignores_mention_only_owner_ping`. |
| Owner DM attachment-only no-instruction message | `passed` | `partial` | Fixture-backed owner DM attachment-only messages submit opaque attachment refs and no raw CDN URL; opaque ingress is model-independent, while fresh live assistant completion and controlled Discord UI attachment smoke are still pending. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_answers_attachment_only_owner_dm_message` and `tests/integration/test_discord_message_acceptance.py::test_discord_dm_attachment_only_message_does_not_blind_read_or_expose_raw_url`. |
| Home-guild attachment-only no-instruction message | `passed` | `partial` | Fixture-backed home-guild attachment-only messages submit opaque attachment refs and no raw CDN URL; opaque ingress is model-independent, while fresh live assistant completion and controlled Discord UI attachment smoke are still pending. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_answers_attachment_only_home_guild_message` and `tests/integration/test_discord_message_acceptance.py::test_discord_attachment_only_message_does_not_blind_read_or_expose_raw_url`. |
| Owner DM attachment read request | `passed` | `partial` | Fixture-backed owner DM attachment-read ingress preserves opaque refs, and API-level reads return typed `scan_failed` in `fail_closed` mode without exposing raw CDN URLs; live read needs main-model-backed tool selection plus a controlled owner attachment and fresh Discord UI smoke. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_answers_owner_dm_attachment_read_request` and `tests/integration/test_discord_message_acceptance.py::test_discord_dm_attachment_read_fail_closed_returns_typed_scan_failure`. |
| Home-guild attachment read request | `passed` | `partial` | Fixture-backed home-guild attachment-read ingress preserves opaque refs, and API-level reads return typed `scan_failed` in `fail_closed` mode without exposing raw CDN URLs; live read needs main-model-backed tool selection plus a controlled owner attachment and fresh Discord UI smoke. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_answers_home_guild_message_in_any_channel` and `tests/integration/test_discord_message_acceptance.py::test_discord_attachment_read_fail_closed_returns_typed_scan_failure`. |
| Origin reply routing | `passed` | `partial` | Fixture-backed Discord-origin worker delivery routes DM and guild replies to the origin; fresh live Discord UI delivery against the current OpenRouter `MAIN` ref has not been recorded. Fixture anchors: `tests/unit/test_worker_discord_delivery.py::test_deliver_to_discord_dm_posts_to_origin_channel_and_replies` and `tests/unit/test_worker_discord_delivery.py::test_deliver_to_discord_guild_message_posts_to_origin_channel`. |
| Default notification delivery | `passed` | `not_run` | Fixture-backed no-origin worker delivery targets the default configured channel. Fixture anchor: `tests/unit/test_worker_discord_delivery.py::test_deliver_to_discord_no_context_uses_default_notification_channel`. |
| `/status` | `passed` | `not_run` | Fixture-backed slash command renders deterministic operational status in guild and owner-DM contexts. Fixture anchors: `tests/unit/test_discord_bot.py::test_slash_status_sends_ephemeral_deterministic_response` and `tests/unit/test_discord_bot.py::test_slash_status_allows_configured_user_dm`. |
| `/jobs` | `passed` | `not_run` | Fixture-backed slash command renders deterministic Agency job status in guild and owner-DM contexts. Fixture anchors: `tests/unit/test_discord_bot.py::test_slash_jobs_sends_ephemeral_deterministic_response` and `tests/unit/test_discord_bot.py::test_slash_jobs_allows_configured_user_dm`. |
| `/capture` | `passed` | `not_run` | Fixture-backed slash command records a capture without background agent work in guild and owner-DM contexts. Fixture anchors: `tests/unit/test_discord_bot.py::test_slash_capture_sends_ephemeral_deterministic_response` and `tests/unit/test_discord_bot.py::test_slash_capture_allows_configured_user_dm`. |
| Wrong-user slash command | `passed` | `not_run` | Fixture-backed `/status`, `/jobs`, and `/capture` wrong-user slash commands reject ephemerally without API mutation. Fixture anchors: `tests/unit/test_discord_bot.py::test_slash_status_rejects_wrong_user`, `tests/unit/test_discord_bot.py::test_slash_jobs_rejects_wrong_user`, and `tests/unit/test_discord_bot.py::test_slash_capture_rejects_wrong_user`. |
| Wrong-guild slash command | `passed` | `not_run` | Fixture-backed `/status`, `/jobs`, and `/capture` wrong-guild slash commands reject ephemerally without API mutation. Fixture anchors: `tests/unit/test_discord_bot.py::test_slash_status_rejects_wrong_guild`, `tests/unit/test_discord_bot.py::test_slash_jobs_rejects_wrong_guild`, and `tests/unit/test_discord_bot.py::test_slash_capture_rejects_wrong_guild`. |
| Wrong-user approval button | `passed` | `not_run` | Fixture-backed approve and deny button clicks from the wrong user reject ephemerally without approval API mutation. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_interaction_rejects_wrong_user`. |
| Wrong-guild approval button | `passed` | `not_run` | Fixture-backed approve and deny button clicks outside the home guild reject ephemerally without approval API mutation. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_interaction_rejects_wrong_guild`. |
| Non-Ariel component custom_id | `passed` | `not_run` | Fixture-backed non-Ariel component custom ids are ignored without API mutation or Discord response. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_interaction_ignores_non_ariel_component_custom_id`. |
| Approval approve button | `passed` | `not_run` | Fixture-backed approval button producer emits the shared approve custom id and the bot posts one approval decision; downstream action execution is owned by the approval API/worker fixture, not by Discord transport. Live UI approval still needs a controlled pending action. Fixture anchors: `tests/unit/test_worker_discord_delivery.py::test_deliver_to_discord_pending_approval_adds_approval_line_and_buttons`, `tests/unit/test_discord_actions.py::test_approval_custom_id_round_trips_supported_decisions`, `tests/unit/test_discord_bot.py::test_on_interaction_handles_approval_custom_id`, and `tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once`. |
| Approval deny button | `passed` | `not_run` | Fixture-backed approval button producer emits the shared deny custom id and the bot posts one denial decision; downstream execution rejection is owned by the approval API fixture, not by Discord transport. Live UI denial still needs a controlled pending action. Fixture anchors: `tests/unit/test_worker_discord_delivery.py::test_deliver_to_discord_pending_approval_adds_approval_line_and_buttons`, `tests/unit/test_discord_actions.py::test_approval_custom_id_round_trips_supported_decisions`, `tests/unit/test_discord_bot.py::test_on_interaction_handles_approval_deny_custom_id`, and `tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_denies_without_enqueuing_execution`. |
| Approval stale/replay interaction | `passed` | `not_run` | Fixture-backed duplicate approval click surfaces the API not-pending error, and the approval API rejects replay without creating another execution task. Fixture anchors: `tests/unit/test_discord_bot.py::test_on_interaction_duplicate_approval_click_surfaces_api_error` and `tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_replay_rejects_without_duplicate_execution`. |
| Malformed approval button custom_id | `passed` | `not_run` | Fixture-backed malformed approval custom ids reject ephemerally without calling the approval API. Fixture anchor: `tests/unit/test_discord_bot.py::test_on_interaction_rejects_invalid_approval_custom_id`. |

### Live Discord Evidence

Run these from the configured owner account and a second non-owner account. Record
message ids, interaction ids, and API row counts only; do not paste private
message content into the smoke log.

1. Confirm the Discord application install includes `bot` and
   `applications.commands`, and that privileged Message Content Intent is
   enabled in the developer portal. Run `/status` and send an owner ambient
   message; both must reach Ariel.
2. Send an owner DM with a harmless status request. Confirm one
   `discord_messages` row and one `user_message` background task or completed
   turn. Confirm the visible response stays in the DM unless the turn
   intentionally finishes silently.
3. Send an owner message in the home guild, outside the default channel if
   available. Confirm it is accepted and routed back to the origin channel or
   thread.
4. Send the same ambient message from a non-owner account in the home guild and
   by DM. Confirm no API turn or background task is created.
5. Send an ambient message from the owner in a non-home guild. Confirm no API
   turn or background task is created.
6. Mention the bot in the home guild with `<@bot_id> smoke mention strip`.
   Confirm the stored prompt excludes the mention token.
7. Send attachment-only owner messages in DM and home-guild contexts with a
   small text fixture and no text instruction. Confirm stored Discord context
   includes attachment refs, raw CDN URLs are absent from model-visible context,
   and Ariel does not blindly read the attachment.
8. Send owner DM and home-guild messages that explicitly ask Ariel to read the
   same controlled attachment. Confirm production `fail_closed` read returns a
   typed scan result unless a clean cached blob or extraction exists.
9. Trigger one controlled no-origin notification. Confirm it is delivered only
   to `ARIEL_DISCORD_CHANNEL_ID`.
10. Run `/status`, `/jobs`, and `/capture` as the owner in the home guild. Confirm
   deterministic ephemeral responses; `/capture` creates a capture row and no
   background task.
11. Run `/status`, `/jobs`, and `/capture` from a non-owner account and in a
   non-home guild. Confirm ephemeral rejection and no API calls.
12. Click one pending approval button from a non-owner account and, if available,
   from a non-home guild copy of the interaction. Confirm ephemeral rejection
   and no API calls.
13. Approve one pending action from its button and deny a second pending action.
    Confirm approve queues execution once, deny queues no execution, and both
    interactions remove or disable their buttons.
14. Replay a stale approval interaction or retry an already-resolved approval
    request through the API-level path. Confirm it reports not-pending without
    duplicating side effects.

## Agent Capability Inventory

Each capability is invoked from a model-authored `run` program through exactly
one syscall name.

The rows below are a source-derived checklist from the capability registry, not
proof that the capability has been exercised in the current pass. Record a
separate evidence state for each row when the capability is run. If a connector,
scope, default location, or test fixture is missing, mark that row `blocked` or
`not_enabled` rather than borrowing evidence from a broader health check.

Eligibility is part of the smoke result:

- Google capabilities surface only when the connector is `connected` and the
  account has the capability's `required_scopes` from the registry. Google write
  and external-send capabilities must use approval flow plus controlled
  create/update/delete fixtures.
- `calendar.propose_slots` is eligible with calendar read scope; attendee
  free/busy intersection needs the optional `calendar.freebusy` reconnect intent.
- Agency capabilities surface only when the Agency runtime is configured and the
  daemon socket is reachable. Production posture additionally requires the
  canonical system `agency-daemon.service` and `/var/lib/agency/agencyd.sock`;
  a healthy user-service socket proves only current-host capability binding, not
  production posture. `agency.run` and `agency.request_pr` must use approval
  flow, and `agency.request_pr` must use only a disposable smoke branch or PR.
- `attachment.read` surfaces only on turns that include attachment refs.
- `search.web`, `web.extract`, `maps.*`, and `weather.forecast` surface only
  when their runtime bindings are configured.
- Memory, proactivity, and research capabilities are local runtime surfaces, but
  their smokes still need worker drainage and typed event/output evidence.

| Capability | Syscall | Preconditions | Smoke evidence |
| --- | --- | --- | --- |
| `cap.calendar.list` | `calendar.list` | Google connected; `calendar.readonly`. | Read upcoming events. |
| `cap.calendar.list_calendars` | `calendar.list_calendars` | Google connected; `calendar.readonly`. | Read visible calendars. |
| `cap.calendar.propose_slots` | `calendar.propose_slots` | Google connected; `calendar.readonly`; `calendar.freebusy` for all-attendee availability. | Propose slots from calendar availability. |
| `cap.calendar.create_event` | `calendar.create_event` | Google connected; `calendar.events`; approval; controlled event and cleanup. | Approval path creates a test event, then cleanup. |
| `cap.calendar.update_event` | `calendar.update_event` | Google connected; `calendar.events`; approval; existing controlled event. | Approval path updates a test event, then cleanup. |
| `cap.calendar.respond_to_event` | `calendar.respond_to_event` | Google connected; `calendar.events`; approval; existing controlled invite. | Approval path responds to a test invite. |
| `cap.email.search` | `email.search` | Google connected; `gmail.readonly`. | Search recent mailbox with bounded results. |
| `cap.email.read` | `email.read` | Google connected; `gmail.readonly`; prior message or thread id. | Read one selected message. |
| `cap.email.draft` | `email.draft` | Google connected; `gmail.compose`; approval; controlled recipient. | Approval path creates a draft. |
| `cap.email.send` | `email.send` | Google connected; `gmail.send`; approval; controlled recipient. | Approval path sends only to a controlled address. |
| `cap.email.archive` | `email.archive` | Google connected; `gmail.modify`; approval; controlled message. | Approval path archives a controlled test message. |
| `cap.email.trash` | `email.trash` | Google connected; `gmail.modify`; approval; controlled message. | Approval path trashes a controlled test message. |
| `cap.email.labels.modify` | `email.labels.modify` | Google connected; `gmail.modify`; approval; controlled message and label. | Approval path labels a controlled test message. |
| `cap.email.undo` | `email.undo` | Google connected; `gmail.modify`; approval; prior reversible receipt. | Reverses a controlled email mutation. |
| `cap.drive.search` | `drive.search` | Google connected; `drive.metadata.readonly`. | Search Drive for a controlled test file. |
| `cap.drive.read` | `drive.read` | Google connected; `drive.readonly`; controlled file id. | Read controlled test file metadata/content. |
| `cap.drive.share` | `drive.share` | Google connected; `drive`; approval; controlled file and grantee. | Approval path shares controlled test file, then cleanup. |
| `cap.maps.directions` | `maps.directions` | Maps binding configured; controlled origin and destination. | Get a route between known addresses. |
| `cap.maps.search_places` | `maps.search_places` | Maps binding configured; controlled query and location context. | Search for a known place near a location. |
| `cap.search.web` | `search.web` | Brave search binding configured; bounded query. | Brave web search returns bounded results. |
| `cap.web.extract` | `web.extract` | Extract binding configured; safe public HTTP(S) URL. | Extract a safe public URL. |
| `cap.weather.forecast` | `weather.forecast` | Weather binding configured; explicit location or canonical default. | Forecast returns bounded structured data. |
| `cap.attachment.read` | `attachment.read` | Current turn has attachment refs; scanner/blob policy determines the typed read outcome. | Read a controlled attachment ref. |
| `cap.agency.run` | `agency.run` | Agency runtime configured; socket reachable; approval; allowed repo root. | Starts an approval-required smoke Agency job. |
| `cap.agency.status` | `agency.status` | Agency runtime configured; existing smoke job id. | Reads smoke Agency job state. |
| `cap.agency.artifacts` | `agency.artifacts` | Agency runtime configured; existing smoke job id with artifacts. | Reads smoke Agency job artifacts. |
| `cap.agency.request_pr` | `agency.request_pr` | Agency runtime configured; approval; smoke branch ready for PR. | Approval path requests a PR for a smoke branch. |
| `cap.memory.recall` | `memory.recall` | Memory runtime configured; active turn context. | Retriever returns relevant memory or empty result. |
| `cap.memory.remember` | `memory.remember` | Memory runtime configured; note content. | Enqueues `memory_encode`; worker records a completed encode turn. |
| `cap.memory.search` | `memory.search` | Memory runtime configured; bounded query. | Searches memory substrate. |
| `cap.memory.read` | `memory.read` | Memory runtime configured; existing memory id. | Reads one memory item. |
| `cap.memory.note.create` | `memory.note.create` | Memory runtime configured; controlled note content. | Creates a note. |
| `cap.memory.note.edit` | `memory.note.edit` | Memory runtime configured; existing controlled note id. | Edits that note. |
| `cap.memory.note.delete` | `memory.note.delete` | Memory runtime configured; existing controlled note id. | Deletes that note. |
| `cap.proactive.schedule` | `proactive.schedule` | Active session; RFC3339 wake time; note content. | Schedules one `agent_wake` row. |
| `cap.research.investigate` | `research.investigate` | Research mode selected; bounded non-poll question. | Enqueues and completes a research run. |

### Capability Evidence Ledger

This ledger separates fixture-backed capability contracts from current-host
smoke evidence. Contract `passed` means the capability behavior is fixture-proven
for this registry surface. Current-host state records only this host's smoke
pass.
Provider-bound read-only rows may be direct capability/provider smokes; they do
not imply main-agent model readiness while `MAIN`, `RESEARCH`, or `VISION`
provider keys are blocked.

| Capability | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `cap.calendar.list` | `passed` | `passed` | Live read-only pass returned a bounded event count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.calendar.list_calendars` | `passed` | `passed` | Live read-only pass returned visible calendar count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.calendar.propose_slots` | `passed` | `partial` | Live execution now has `calendar.freebusy`, but the controlled attendee check returned `availability_scope=primary_calendar_only`, `partial=true`, and free/busy diagnostic `missing_calendar`. All-attendee availability remains unproven. Fixture anchors: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`, `tests/integration/test_google_connector_read_acceptance.py::test_calendar_propose_slots_uses_freebusy_scope_for_all_attendees`, and `tests/integration/test_google_connector_read_acceptance.py::test_attendee_slots_are_limited_scope_and_recoverable_without_freebusy_scope`. |
| `cap.email.search` | `passed` | `passed` | Live read-only pass returned bounded Gmail message count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.email.read` | `passed` | `passed` | Live read-only pass read one selected message and returned `read_outcome=ok` without printing message content; fixture anchors: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval` and `tests/integration/test_google_connector_read_acceptance.py::test_email_read_thread_mode_executes_allowlisted_without_approval`. |
| `cap.drive.search` | `passed` | `passed` | Live action-runtime read pass returned bounded Drive search results through the connected Google account; fixture anchors: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_search_and_read_execute_inline_with_retrieval_citations` and `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_provider_failures_are_typed_and_recoverable`. |
| `cap.drive.read` | `passed` | `partial` | Live action-runtime read pass reached a selected Drive file and returned a typed `read_outcome=too_large` provider response without content exposure; a controlled small-file `read_outcome=ok` read remains unproven. Fixture anchors: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_search_and_read_execute_inline_with_retrieval_citations`, `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_read_typed_outcomes_are_explicit_and_recoverable`, and `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_provider_failures_are_typed_and_recoverable`. |
| `cap.maps.directions` | `passed` | `passed` | Live Google Routes call succeeded from this host with two route candidates for Pike Place Market to SEA; fixture anchor: `tests/integration/test_maps_acceptance.py::test_maps_directions_executes_against_routes_api_with_citations`. |
| `cap.maps.search_places` | `passed` | `passed` | Live Google Places and Geocoding calls succeeded from this host with five bounded place results for coffee near Downtown Seattle; fixture anchor: `tests/integration/test_maps_acceptance.py::test_maps_search_places_executes_against_places_api_with_metadata`. |
| `cap.calendar.create_event` | `passed` | `not_run` | Live connector now has `calendar.events`, but approval-gated event creation was not run in the current pass; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_calendar_create_requires_approval_and_executes_exactly_once`. |
| `cap.calendar.update_event` | `passed` | `not_run` | Live connector now has `calendar.events`, but no controlled live event update was run; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority`. |
| `cap.calendar.respond_to_event` | `passed` | `not_run` | Live connector now has `calendar.events`, but no controlled live invite response was run; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority`. |
| `cap.email.draft` | `passed` | `not_run` | Live connector now has `gmail.compose`, but approval-gated draft creation was not run in the current pass; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_email_draft_queues_then_executes_as_draft_only_without_send_side_effect`. |
| `cap.email.send` | `passed` | `not_run` | Live connector now has `gmail.send`, but no controlled send was run in the current pass; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_email_send_requires_approval_and_executes_exactly_once`. |
| `cap.email.archive` | `passed` | `not_run` | Live connector now has `gmail.modify`, but no controlled archive mutation was run; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_action_success_redacts_undo_token_from_event_audit`. |
| `cap.email.trash` | `passed` | `not_run` | Live connector now has `gmail.modify`, but no controlled trash mutation was run; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token`. |
| `cap.email.labels.modify` | `passed` | `not_run` | Live connector now has `gmail.modify`, but no controlled label mutation was run; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token`. |
| `cap.email.undo` | `passed` | `not_run` | Live connector now has `gmail.modify`, but no live reversible email mutation receipt exists from this smoke pass; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_undo_marks_prior_receipt_undone_on_the_single_ledger`. |
| `cap.drive.share` | `passed` | `not_run` | Live connector now has the `drive` scope, but approval-gated Drive sharing was not run in the current pass; fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_share_is_approval_gated_exact_payload_and_exactly_once`. |
| `cap.web.extract` | `passed` | `passed` | Live Jina Reader (`https://r.jina.ai`) returns structured markdown for the smoke URL with bearer auth from `ARIEL_JINA_API_KEY`. Fixture anchor: `tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance`. |
| `cap.search.web` | `passed` | `passed` | Live Brave provider returned bounded web results; fixture anchor: `tests/integration/test_search_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations`. |
| `cap.weather.forecast` | `passed` | `passed` | Live Tomorrow.io provider returned a bounded forecast; fixture anchor: `tests/integration/test_search_weather_acceptance.py::test_weather_explicit_location_wins_and_response_contains_location_timeframe_and_timestamps`. |
| `cap.attachment.read` | `passed` | `blocked` | Capability is configured, but the live DB has zero `attachment_sources`, `attachment_blobs`, or `attachment_extractions`. A real smoke needs an owner Discord message with a controlled small attachment and explicit read intent; with current `ARIEL_ATTACHMENT_SCANNER_MODE=fail_closed`, the production expected read result is a typed `scan_failed` outcome unless a clean cached blob or scanner backend exists. Fixture anchors: `tests/integration/test_attachment_content_runtime.py::test_attachment_read_returns_scanner_gate_failures_without_persisting_content`, `tests/integration/test_attachment_content_runtime.py::test_attachment_read_failure_contract_returns_typed_recovery`, `tests/integration/test_attachment_content_runtime.py::test_attachment_read_image_and_pdf_use_vision_model_ref`, `tests/integration/test_discord_message_acceptance.py::test_discord_attachment_read_tool_reads_text_attachment`, and `tests/integration/test_discord_message_acceptance.py::test_discord_attachment_read_fail_closed_returns_typed_scan_failure`. |
| `cap.agency.run` | `passed` | `blocked` | Historical host settings reached the user-service Agency socket at `/home/niels/.local/share/agency/agencyd.sock`, but canonical system Agency is absent and no fresh live run was started after local edits because it would launch a real model runner against the dirty Ariel checkout. Run only after canonical `/opt/ariel` posture or an operator-approved disposable smoke repo/branch is available. Fixture anchor: `tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once`. |
| `cap.agency.status` | `passed` | `blocked` | Historical user-service Agency health was reachable, but the current blocker is that no tracked daemon-linked smoke job exists to read and no fresh status smoke has been run after local edits. This is a smoke-shape/local-posture blocker, not an external provider blocker; unblock by completing the controlled `agency.run` smoke first. Fixture anchor: `tests/integration/test_agency_runtime_capabilities.py::test_agency_status_and_artifacts_execute_against_daemon_and_update_job`. |
| `cap.agency.artifacts` | `passed` | `blocked` | Historical user-service Agency health was reachable, but the current blocker is that no tracked daemon-linked smoke job with artifacts exists to read and no fresh artifacts smoke has been run after local edits. This is a smoke-shape/local-posture blocker, not an external provider blocker; unblock by completing the controlled `agency.run` smoke first. Fixture anchor: `tests/integration/test_agency_runtime_capabilities.py::test_agency_status_and_artifacts_execute_against_daemon_and_update_job`. |
| `cap.agency.request_pr` | `passed` | `blocked` | Current host has no tracked Agency smoke job, and the capability requires an operator-approved disposable PR side effect. Do not run against the dirty Ariel checkout or merge the smoke PR. Fixture anchor: `tests/integration/test_agency_receipt_reconcile.py::test_agency_request_pr_receipt_ids_are_replayed_without_daemon_call`. |
| `cap.memory.recall` | `passed` | `passed` | Live direct run-program smoke returned `status=recalled` through the real gVisor sandbox and model-backed retriever; fixture anchor: `tests/integration/test_memory.py::test_memory_recall_syscall_runs_retriever_inline`. |
| `cap.memory.remember` | `passed` | `passed` | Live direct run-program smoke queued `memory_encode`; restarted `ariel-worker` drained it and recorded a completed encode turn; fixture anchors: `tests/integration/test_memory.py::test_memory_remember_enqueues_memory_encode_task` and `tests/integration/test_memory.py::test_memory_remember_enqueues_and_worker_records_encode_turn`. |
| `cap.memory.search` | `passed` | `passed` | Live direct run-program smoke searched for a controlled note marker and returned the note id before cleanup; fixture anchor: `tests/integration/test_run_program_runtime.py::test_program_reads_a_capability_then_composes_an_emit_message` seeds a known memory row and asserts it appears in the syscall result. |
| `cap.memory.read` | `passed` | `passed` | Live direct run-program smoke read the controlled note before and after edit; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.create` | `passed` | `passed` | Live direct run-program smoke created a controlled note; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.edit` | `passed` | `passed` | Live direct run-program smoke edited the controlled note; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.delete` | `passed` | `passed` | Live direct run-program smoke deleted the controlled note; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.proactive.schedule` | `passed` | `partial` | Live direct run-program smoke created a far-future `agent_wake` task, then cleaned it up to avoid an unintended wake; worker-drained due wake still needs a controlled live pass. Fixture anchors: `tests/integration/test_proactivity_scheduler.py::test_schedule_syscall_writes_an_agent_wake_background_task`, `tests/integration/test_proactivity_scheduler.py::test_worker_agent_wake_arm_invokes_wake_for_a_due_task`, and `tests/integration/test_proactivity_scheduler.py::test_schedule_run_program_worker_drains_due_wake_end_to_end`. |
| `cap.research.investigate` | `passed` | `partial` | Live direct run-program returned `status=queued` with a research id inside a rolled-back transaction; no durable live task remained, avoiding an unbounded live research worker job. Worker completion and follow-up wake still need a bounded live pass. Fixture anchors: `tests/integration/test_research_wiring.py::test_research_investigate_syscall_enqueues_a_research_run_task`, `tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake`, `tests/integration/test_research_wiring.py::test_worker_completion_wake_renders_finding_into_main_agent_context`, and `tests/integration/test_research_wiring.py::test_research_investigate_run_program_worker_drains_research_and_completion_wake_end_to_end`. |

### Agency Local Preflight

This preflight is read-only. It proves only current-host Agency binding shape and
must not be used to mark `agency.run`, `agency.status`, `agency.artifacts`, or
`agency.request_pr` passed. Do not call `task_start`, `land_invocation`,
`worktree_pr_sync`, or any other daemon mutation during this preflight.

```sh
.venv/bin/python - <<'PY'
from pathlib import Path

from sqlalchemy import create_engine, text

from ariel.agency_daemon import AGENCY_DAEMON_API_VERSION, AgencyDaemonClient
from ariel.config import AppSettings

settings = AppSettings()
print(
    {
        "configured_socket_path": settings.agency_socket_path,
        "allowed_repo_roots": settings.agency_allowed_repo_roots,
        "default_base_branch": settings.agency_default_base_branch,
        "default_runner": settings.agency_default_runner,
        "client_api_version": AGENCY_DAEMON_API_VERSION,
    }
)
canonical_socket = Path("/var/lib/agency/agencyd.sock")
print({"canonical_socket_exists": canonical_socket.exists()})

configured_socket = Path(settings.agency_socket_path)
if configured_socket.exists():
    try:
        health = AgencyDaemonClient(
            socket_path=settings.agency_socket_path,
            timeout_seconds=settings.agency_timeout_seconds,
        ).health()
    except Exception as exc:
        print({"configured_socket_health": "failed", "error_type": exc.__class__.__name__})
    else:
        print(
            {
                "configured_socket_health": "ok",
                "api_version": health.get("api_version"),
            }
        )
else:
    print({"configured_socket_health": "missing"})

engine = create_engine(str(settings.database_url), future=True)
with engine.connect() as db:
    daemon_job_count = db.scalar(
        text("select count(*) from jobs where source = 'agency.daemon'")
    )
print({"agency_daemon_job_count": int(daemon_job_count or 0)})
PY
systemctl is-active agency-daemon --no-pager || true
systemctl is-enabled agency-daemon --no-pager || true
```

### Agency Capability Smoke

Use this only after canonical Agency production posture passes or the operator
has explicitly approved a disposable smoke repo/branch. A healthy user-service
socket is enough to prove current-host capability binding, but it is not enough
to mark production posture passed.

1. Confirm either canonical `make production-posture` passes for
   `agency-daemon.service` and `/var/lib/agency/agencyd.sock`, or the operator
   explicitly approves a disposable user-service Agency smoke. Separately
   verify `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` and `ARIEL_AGENCY_DEFAULT_RUNNER`
   from the active Ariel settings before starting a job.
2. Start a single owner-approved `agency.run` with `no_include_untracked=true`,
   an allowed repo root, explicit base branch, explicit runner, and a prompt
   that may touch only a smoke-only file or branch.
3. Approve through the normal approval path, wait for `ariel-worker` to drain
   the action attempt, then verify one new daemon-linked job row, job timeline
   events, `agency.status(job_id=...)`, and `agency.artifacts(job_id=...)`.
4. Run `agency.request_pr(job_id=...)` only if the branch and remote PR are
   disposable and the operator has approved the external-send side effect.
   Verify the PR URL, then close the PR unmerged and delete the smoke branch.
5. Clean up the Agency worktree/task through the Agency cleanup surface and
   record every job id, PR URL, and cleanup result in the ledger.

### Provider Fixture Smoke

These no-secret tests exercise provider adapters and egress policy with faked
HTTP/provider seams. They do not replace live-provider checks; live rows remain
`blocked` or `not_enabled` until real bindings are configured and exercised.

| Surface | Fixture smoke |
| --- | --- |
| `search.web` | `tests/integration/test_search_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations` |
| `search.web` egress | `tests/integration/test_search_weather_acceptance.py::test_search_web_egress_fails_closed_before_execute` |
| `weather.forecast` dev adapter | `tests/unit/test_capability_registry_search.py::test_weather_dev_adapter_parses_wttr_payload_without_api_key` |
| `weather.forecast` production adapter | `tests/unit/test_capability_registry_search.py::test_weather_production_adapter_parses_tomorrow_io_payload` and `tests/unit/test_capability_registry_search.py::test_weather_production_adapter_preserves_lat_lon_location_param` |
| `maps.directions` | `tests/integration/test_maps_acceptance.py::test_maps_directions_executes_against_routes_api_with_citations` |
| `maps.search_places` | `tests/integration/test_maps_acceptance.py::test_maps_search_places_executes_against_places_api_with_metadata` |
| `web.extract` | `tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance` |

### Research Mode Evidence Ledger

`cap.research.investigate` has three runtime modes plus guardrails for invalid
or cross-domain requests. A passed capability row does not imply every mode ran
on the current host.

| Research mode or guard | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `web` | `passed` | `partial` | Fixture anchors cover queued research and worker drainage: `tests/integration/test_research_wiring.py::test_research_investigate_syscall_enqueues_a_research_run_task` and `tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake`; current live smoke proved only bounded queue shape. |
| `personal` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_research_runtime.py::test_run_research_personal_mode_exposes_only_personal_capabilities`; current live smoke did not run a Google Workspace personal research job. |
| `memories` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_research_runtime.py::test_run_research_memories_mode_exposes_only_memory_capabilities`; current live smoke did not run a memory-only research job. |
| Invalid research mode | `passed` | `not_run` | Validator-backed runtime rejects modes outside `RESEARCH_MODE_VALUES`; fixture anchor: `tests/integration/test_research_wiring.py::test_research_investigate_syscall_rejects_a_bad_mode`. |
| Cross-mode private/web rejection | `passed` | `not_run` | Fixture-backed research runtime keeps private Google and memory work out of web-only mode and keeps public web extraction out of private-only modes. Fixture anchors: `tests/integration/test_research_runtime.py::test_run_research_web_mode_exposes_only_web_capabilities`, `tests/integration/test_research_runtime.py::test_run_research_web_mode_program_cannot_call_personal_capability`, and `tests/integration/test_research_runtime.py::test_run_research_personal_mode_exposes_only_personal_capabilities`. |

### AI Judgment Evidence Ledger

`ai_judgments` is an audit ledger for bounded model calls. It is not product
state and must not become a decision source.

| Judgment type | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `memory_recall` | `passed` | `not_run` | Schema guard constrains the judgment type; fixture anchor: `tests/integration/test_memory.py::test_ai_judgments_accepts_persisted_types_and_rejects_unknown_type`. Current live memory recall completed, but a row-specific `ai_judgments` audit query was not recorded. |
| `memory_encode` | `passed` | `not_run` | Schema guard constrains the judgment type; fixture anchor: `tests/integration/test_memory.py::test_ai_judgments_accepts_persisted_types_and_rejects_unknown_type`. Current live worker drained a `memory_encode` task, but a row-specific `ai_judgments` audit query was not recorded. |
| `memory_dream` | `passed` | `not_run` | Schema guard constrains the judgment type; fixture anchor: `tests/integration/test_memory.py::test_ai_judgments_accepts_persisted_types_and_rejects_unknown_type`. |
| `model_output` | `passed` | `not_run` | Schema guard constrains the judgment type; fixture anchor: `tests/integration/test_memory.py::test_ai_judgments_accepts_persisted_types_and_rejects_unknown_type`. Current live main-agent completion and row-specific `ai_judgments` audit have not been recorded against the OpenRouter `MAIN` ref. |

### Agent Loop Rail Evidence Ledger

These rows prove host-side loop rails separately from capability behavior.

| Rail | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| Wall-clock budget | `passed` | `not_run` | Fixture anchor: `tests/integration/test_agent_loop_exhaustion_acceptance.py::test_turn_budget_exhaustion_ends_gracefully`. |
| Model-call backstop | `passed` | `not_run` | Fixture anchor: `tests/integration/test_agent_loop_exhaustion_acceptance.py::test_model_call_backstop_exhaustion_ends_gracefully`. |
| Stuck detection | `passed` | `not_run` | Fixture anchor: `tests/integration/test_agent_loop_exhaustion_acceptance.py::test_stuck_detection_ends_turn_gracefully`. |
| Per-program commit | `passed` | `partial` | Fixture anchors: `tests/integration/test_normal_turn_program_loop.py::test_program_that_raises_is_a_program_failure` and `tests/integration/test_normal_turn_program_loop.py::test_two_programs_with_capability_syscalls_get_distinct_proposal_index`; current live direct-run smokes committed memory and proactivity side effects. |
| Failed-program approval voiding | `passed` | `not_run` | Fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_program_that_raises_is_a_program_failure`. |
| Task replay | `passed` | `not_run` | Fixture anchor: `tests/integration/test_session_management_acceptance.py::test_message_idempotency_key_replays_same_task_id`. |
| emit_value eviction | `passed` | `not_run` | Fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_emit_value_eviction_discards_prior_round`. |
| Remaining-budget signal | `passed` | `not_run` | Fixture coverage in `tests/integration/test_normal_turn_program_loop.py::test_main_agent_prompt_is_static_prefix_before_dynamic_context` validates the dynamic `remaining budget:` line without mutating the static prompt prefix. |
| Run-protocol recovery | `passed` | `not_run` | Fixture coverage in `tests/integration/test_normal_turn_program_loop.py::test_plain_assistant_text_is_protocol_feedback_not_visible` and `tests/integration/test_normal_turn_program_loop.py::test_invalid_direct_tool_protocol_retries_without_executing` validates protocol errors feed back to the model without visible fallback prose or side effects. |
| Retryable model error retry | `partial` | `not_run` | Fixture coverage in `tests/integration/test_agent_loop_exhaustion_acceptance.py::test_model_call_backstop_exhaustion_ends_gracefully` exercises retryable provider errors through the graceful exhaustion path; a success-after-retry fixture is still a coverage gap. |
| Round-history eviction | `passed` | `not_run` | Fixture coverage in `tests/integration/test_normal_turn_program_loop.py::test_emit_value_eviction_discards_prior_round` validates superseded round history is pruned from later model input. |
| Scratch bounds/taint | `passed` | `not_run` | Fixture anchors: `tests/integration/test_run_runtime_scratch.py::test_scratch_set_and_get_round_trip`, `tests/integration/test_run_runtime_scratch.py::test_scratch_set_taint_propagates_on_get`, `tests/integration/test_run_runtime_scratch.py::test_scratch_set_rejects_too_large_value`, `tests/integration/test_run_runtime_scratch.py::test_scratch_set_rejects_invalid_key`, `tests/integration/test_run_runtime_scratch.py::test_scratch_store_full_rejects_excess_entries`, and `tests/integration/test_run_runtime_scratch.py::test_scratch_total_bytes_cap_rejects_excess`. |

## Agent Tool Inventory

The model-facing tool surface is intentionally one tool: `run`. Capabilities and
runtime controls are callable names inside the sandboxed run program, not
separate model tools.

| Tool | Owner | Smoke evidence |
| --- | --- | --- |
| `run` | `src/ariel/run_runtime.py` | Model requests expose exactly one strict `run` tool; parser rejects missing, duplicate, wrong-name, blank, and oversized tool calls. |

### Agent Tool Evidence Ledger

| Tool | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `run` | `passed` | `partial` | Current targeted fixture tests prove the local strict tool surface and parser envelope: `tests/unit/test_responses_tool_contract.py::test_normal_response_tool_surface_is_single_strict_run_tool`, `tests/unit/test_responses_tool_contract.py::test_run_protocol_requires_exactly_one_run_call`, and `tests/unit/test_responses_tool_contract.py::test_run_source_rejects_blank_and_oversized_programs`; main-agent completion still needs a fresh smoke against the current OpenRouter `MAIN` ref. |

## Model Runtime Syscall Inventory

These syscalls are always host-provided runtime controls, not capabilities.

| Syscall | Smoke evidence |
| --- | --- |
| `agent.emit_message` | Main-agent run can complete with exactly one user-visible message. |
| `agent.emit_value` | Multi-round run can emit bounded internal state. |
| `agent.finish_silent` | Run can finish silently without visible assistant text. |
| `agent.emit_finding` | Research or retriever run can emit a typed finding; main-agent misuse is rejected. |
| `agent.emit_done` | Rememberer run can end without a user-visible message; main-agent misuse is rejected. |
| `scratch.set` | Program stores a bounded JSON value for the current turn. |
| `scratch.get` | Program reads a scratch value and preserves taint provenance. |

### Model Runtime Syscall Evidence Ledger

These rows prove runtime-control syscalls separately from capability syscalls.
Current-host `not_run` means the contract is fixture-proven but was not repeated
against this host in the current smoke pass.

| Syscall | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `agent.emit_message` | `passed` | `partial` | Current targeted fixture tests prove host-side message emission through direct run-program and loop recovery paths: `tests/integration/test_run_program_runtime.py::test_program_reads_a_capability_then_composes_an_emit_message` and `tests/integration/test_main_loop_recovery.py::test_main_loop_pure_emit_message_round_one_is_not_dropped`; direct-message completion still needs a fresh smoke against the current OpenRouter `MAIN` ref. |
| `agent.emit_value` | `passed` | `partial` | Current targeted fixture tests prove digest-only internal feedback and next-round fact carryover: `tests/integration/test_normal_turn_program_loop.py::test_emit_value_is_internal_feedback_with_digest_surface` and `tests/integration/test_main_loop_recovery.py::test_emit_value_carries_read_facts_to_next_round_context`. |
| `agent.finish_silent` | `passed` | `partial` | Current targeted fixture test proves silent finish without visible output: `tests/integration/test_normal_turn_program_loop.py::test_finish_silent_ends_turn_without_visible_output`. |
| `agent.emit_finding` | `passed` | `partial` | Current targeted fixture tests prove research finding emission and main-agent misuse rejection: `tests/integration/test_run_runtime_research_finding.py::test_research_finding_happy_path_sets_emitted_finding` and `tests/integration/test_run_runtime_research_finding.py::test_emit_finding_rejected_in_main_agent_run`. |
| `agent.emit_done` | `passed` | `partial` | Current targeted fixture tests prove rememberer completion and main-loop misuse recovery: `tests/integration/test_memory.py::test_memory_remember_enqueues_and_worker_records_encode_turn` and `tests/integration/test_main_loop_recovery.py::test_main_loop_emit_done_misuse_recovers_with_typed_nudge`. |
| `scratch.set` | `passed` | `partial` | Current targeted fixture tests prove scratch round-trip and taint propagation: `tests/integration/test_run_runtime_scratch.py::test_scratch_set_and_get_round_trip` and `tests/integration/test_run_runtime_scratch.py::test_scratch_set_taint_propagates_on_get`. |
| `scratch.get` | `passed` | `partial` | Current targeted fixture tests prove scratch round-trip and missing-key behavior: `tests/integration/test_run_runtime_scratch.py::test_scratch_set_and_get_round_trip` and `tests/integration/test_run_runtime_scratch.py::test_scratch_get_missing_key_returns_error`. |

## Background Work Inventory

| Worker path | Expected behavior | Smoke evidence |
| --- | --- | --- |
| `ariel-worker` | Drains or re-arms selected due `background_tasks` without blocking behind one provider sync. | Due rows disappear or re-arm; failures back off; no stale due rows or waiting advisory-lock backends remain. |
| Direct `user_message` task | Runs the main agent for API and Discord-originated input. | Message task disappears, turn completes, idempotency outcome is recorded, and Discord origin routing is honored when present. |
| Scheduled `agent_wake` | Runs the main agent loop. | Session turn appears and Discord notification is sent when targeted. |
| Provider event ingest | Accepts Calendar webhook events and Gmail Pub/Sub notifications, then queues resource sync work. | `provider_events` and a follow-up `provider_sync_due` row are recorded; the later sync task owns `sync_runs` and cursor outcomes. |
| Provider reconcile poll | Enqueues backstop sync work for connected Google cursors. | One-shot `provider_sync_due` rows appear and the recurring poll row re-arms; the later sync task owns cursor advancement. |
| `research_run` | Runs bounded research and schedules a completion wake. | Research row is consumed, finding is recorded, and a completion `agent_wake` is enqueued. |
| `provider_write_reconcile_due` | Reconciles ambiguous provider-write receipts. | Reconcile row is consumed or retried with preserved receipt identity. |
| Stale cursor full-resync recovery | Clears stale Gmail or Calendar cursors and enqueues a fresh full sync. | Cursor value is cleared, typed stale-cursor error is recorded, and one replacement `provider_sync_due` appears. |
| Watch renewal | Renews expiring Google watches before expiry. | Watch expiry moves forward and the recurring renewal row re-arms. |
| Google connector-error wake | Surfaces connector token-refresh failures to the user. | A connector-error `agent_wake` is enqueued without registering watches. |
| Recurring maintenance seeders | Recreate missing recurring provider-maintenance and approval-expiry rows exactly once. | Missing rows are seeded without duplicates. |
| Worker one-shot failure retry/exhaustion | Logs task-arm exceptions, backs off retries, and abandons exhausted one-shot tasks. | Failed one-shot rows retry with error logs, then delete after the max attempts. |
| Worker recurring failure exhaustion re-arm | Logs task-arm exceptions, backs off retries, and re-arms exhausted recurring tasks. | Failed recurring rows retry with error logs, then re-arm for the next recurrence after the max attempts. |
| `memory_encode` | Writes memory assertions/notes. | Memory log records the encoded result. |
| Memory dream | Runs recurring consolidation. | Dream row re-arms after successful run. |
| Durable action execution | Executes approved actions once. | Receipts and action attempts reach a final replay-safe state. |
| Approval expiry | Expires old approvals. | Expired approval cannot execute. |
| Agency event ingest | Reconciles Agency event state. | Heartbeats mark processed; job events update durable jobs; waiting and terminal job events wake the agent. |
| `ariel-pubsub` | Pulls Gmail Pub/Sub notifications. | Fresh subscriber heartbeat; malformed immutable payloads are acked and dropped. |
| `ariel-discord` | Discord gateway ingress, slash commands, and approval buttons. | Gateway connected; slash commands registered. |

### Background Work Evidence Ledger

| Worker path | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `ariel-worker` | `passed` | `partial` | Static guard fixtures prove every schema task type has a worker match arm, and row-specific fixtures cover selected behavioral dispatch paths; live worker drained `memory_encode`, but canonical production systemd posture is not installed yet. |
| Direct `user_message` task | `passed` | `partial` | Fixture anchor: `tests/integration/test_session_management_acceptance.py::test_message_idempotency_key_replays_same_task_id`; historical live route accepted a `user_message` task, but fresh completion against the current OpenRouter `MAIN` ref has not been recorded. |
| Scheduled `agent_wake` | `passed` | `blocked` | Fixture anchors: `tests/integration/test_proactivity_scheduler.py::test_worker_agent_wake_arm_invokes_wake_for_a_due_task` and `tests/integration/test_research_wiring.py::test_worker_completion_wake_renders_finding_into_main_agent_context`; live due wake completion needs the main model key. |
| Provider event ingest | `passed` | `not_run` | Fixture anchor: `tests/integration/test_discord_primary_durable_workflows_acceptance.py::test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe`; fixture coverage stores one provider event and enqueues one exact follow-up sync task. The public invalid-webhook smoke proves only rejection, not a positive provider event. |
| Provider reconcile poll | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_reconcile_handler_enqueues_provider_sync_due_for_each_cursor` and `tests/integration/test_google_provider_ingestion.py::test_worker_provider_reconcile_sync_due_arm_enqueues_sync_tasks_and_rearms`; generic sync-run and cursor rows do not by themselves prove a current positive reconcile poll. |
| `research_run` | `passed` | `partial` | Fixture anchor: `tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake`; live smoke only proved queued shape. |
| `provider_write_reconcile_due` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_worker_background_tasks.py::test_worker_provider_write_reconcile_due_arm_reconciles_and_deletes_task`, `tests/integration/test_worker_background_tasks.py::test_worker_provider_write_reconcile_due_retries_bad_task_shape`, `tests/integration/test_agency_receipt_reconcile.py::test_agency_request_pr_ambiguous_receipt_reconciles_with_preserved_identity`, `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_update_ambiguous_receipt_reconciles_with_readback`, and `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_rsvp_ambiguous_receipt_reconciles_with_readback`. |
| Stale cursor full-resync recovery | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_calendar_410_clears_cursor_and_reenqueues_full_sync` and `tests/integration/test_google_provider_ingestion.py::test_gmail_404_clears_cursor_and_reenqueues_full_sync`. |
| Watch renewal | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_watch_renew_handler_rearms_near_expiry_channel` and `tests/integration/test_google_provider_ingestion.py::test_worker_provider_watch_renew_due_arm_renews_and_rearms`. |
| Google connector-error wake | `passed` | `not_run` | Fixture anchor: `tests/integration/test_google_provider_ingestion.py::test_watch_renew_handler_token_refresh_failure_enqueues_connector_error_wake`. |
| Recurring maintenance seeders | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_seed_provider_maintenance_tasks_creates_recurring_rows_once` and `tests/integration/test_google_provider_ingestion.py::test_seed_approval_expiry_task_creates_recurring_row_once`. |
| Worker one-shot failure retry/exhaustion | `passed` | `not_run` | Fixture anchors: `tests/integration/test_worker_failure_logging.py::test_exception_in_arm_logs_traceback_with_task_type` and `tests/integration/test_worker_failure_logging.py::test_repeated_failures_eventually_delete_one_shot_task`. |
| Worker recurring failure exhaustion re-arm | `passed` | `not_run` | Fixture anchor: `tests/integration/test_worker_failure_logging.py::test_repeated_failures_rearm_recurring_task`. |
| `memory_encode` | `passed` | `passed` | Fixture anchors: `tests/integration/test_memory.py::test_memory_remember_enqueues_memory_encode_task` and `tests/integration/test_memory.py::test_memory_remember_enqueues_and_worker_records_encode_turn`; live worker drained a `memory_encode` task. |
| Memory dream | `passed` | `not_run` | Fixture anchors: `tests/integration/test_memory.py::test_worker_accepts_memory_encode_and_memory_dream` and `tests/integration/test_memory.py::test_worker_memory_dream_task_inserts_turn_against_system_session`. |
| Durable action execution | `passed` | `not_run` | Fixture anchors: `tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once` and `tests/integration/test_google_connector_write_acceptance.py::test_calendar_create_requires_approval_and_executes_exactly_once`; no provider-mutating live write drill was run in this pass. |
| Approval expiry | `passed` | `not_run` | Fixture anchors: `tests/integration/test_worker_background_tasks.py::test_expire_approvals_task_expires_pending_once_and_rearms` and `tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_expired_approval_rejects_without_execution`. |
| Agency event ingest | `passed` | `partial` | Fixture anchor: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_upserts_job_event_wake_and_deletes_task`; live signed event produced terminal job events, while heartbeat, malformed, non-waking, and waiting event classes remain fixture-backed. |
| `ariel-pubsub` | `passed` | `passed` | Fixture anchor: `tests/integration/test_pubsub_subscriber.py::test_handle_message_happy_path`; live health reported a fresh subscriber heartbeat. |
| `ariel-discord` | `passed` | `partial` | Fixture anchors: `tests/unit/test_discord_bot.py::test_on_message_answers_configured_user_dm` and `tests/integration/test_discord_message_acceptance.py::test_no_visible_response_operation_completes_turn_without_visible_reply`; live gateway configuration is present, while message-completion rows own main-model completion state. |

## Background Task Type Inventory

Every persisted `background_tasks.task_type` must be accepted by the schema and
dispatched by `ariel-worker`.

| Task type | Expected behavior | Smoke evidence |
| --- | --- | --- |
| `agency_event_received` | Processes signed Agency events. | Heartbeats mark processed without jobs; job events update durable jobs/events; waiting and terminal job events wake the agent. |
| `agent_wake` | Runs the main agent from a note or research-completion wake. | Due wake task completes and creates a turn; targeted wakes notify Discord. |
| `research_run` | Runs bounded research and schedules an agent wake with the finding. | Research task completes, records finding, and enqueues follow-up wake. |
| `user_message` | Runs the main agent for direct or Discord-originated user input. | Message task disappears and the latest turn completes. |
| `execute_action_attempt` | Executes an approved action exactly once. | Approved action reaches a terminal attempt and receipt state. |
| `provider_write_reconcile_due` | Reconciles ambiguous provider-write receipts. | Queued reconcile task deletes on success or retries on transient provider state. |
| `expire_approvals` | Expires pending approvals past their TTL. | Expired approval is marked expired and cannot execute. |
| `provider_event_received` | Turns a normalized Google push event into provider sync work. | Push event is accepted and one `provider_sync_due` task is enqueued; the sync task later marks the event processed or failed. |
| `provider_sync_due` | Runs Gmail and Calendar sync; accepts Drive cursor work without Drive item ingestion until Drive delta handling is implemented. | Sync run is recorded and cursor state advances or records a typed error; Drive change processing is currently a no-op. |
| `memory_encode` | Runs the rememberer for an explicit note. | Memory log/note rows record the encoded result. |
| `memory_dream` | Runs recurring memory consolidation. | Recurring task re-arms after the dream run. |
| `provider_watch_renew_due` | Renews expiring Google watches. | Recurring task re-arms; watch expiry moves forward when renewal is due. |
| `provider_reconcile_sync_due` | Enqueues backstop sync tasks for connected Google cursors. | Recurring task re-arms and one-shot `provider_sync_due` rows are enqueued. |

### Background Task Type Evidence Ledger

| Task type | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| `agency_event_received` | `passed` | `partial` | Fixture anchors: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_heartbeat_marks_processed_without_job_or_wake`, `tests/integration/test_worker_background_tasks.py::test_agency_event_received_progress_updates_job_without_wake`, `tests/integration/test_worker_background_tasks.py::test_agency_event_received_waiting_wakes_agent_for_approval_state`, `tests/integration/test_worker_background_tasks.py::test_agency_event_received_upserts_job_event_wake_and_deletes_task`, and `tests/integration/test_worker_background_tasks.py::test_agency_event_received_missing_job_id_fails_event_and_retries_task`; live signed event task completed into terminal job events, while other event classes remain fixture-backed. |
| `agent_wake` | `passed` | `partial` | Fixture anchors: `tests/integration/test_proactivity_scheduler.py::test_worker_agent_wake_arm_invokes_wake_for_a_due_task` and `tests/integration/test_research_wiring.py::test_worker_completion_wake_renders_finding_into_main_agent_context`; live due wake completion needs a fresh smoke against the current OpenRouter `MAIN` ref. |
| `research_run` | `passed` | `partial` | Fixture anchors: `tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake` and `tests/integration/test_research_wiring.py::test_research_investigate_run_program_worker_drains_research_and_completion_wake_end_to_end`; live smoke only proved queued shape. |
| `user_message` | `passed` | `partial` | Fixture anchor: `tests/integration/test_session_management_acceptance.py::test_message_idempotency_key_replays_same_task_id`; historical live route accepted the task, but fresh completion against the current OpenRouter `MAIN` ref has not been recorded. |
| `execute_action_attempt` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once` and `tests/integration/test_google_connector_write_acceptance.py::test_calendar_create_requires_approval_and_executes_exactly_once`; provider-mutating live write drill was not run. |
| `provider_write_reconcile_due` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_worker_background_tasks.py::test_worker_provider_write_reconcile_due_arm_reconciles_and_deletes_task`, `tests/integration/test_worker_background_tasks.py::test_worker_provider_write_reconcile_due_retries_bad_task_shape`, `tests/integration/test_agency_receipt_reconcile.py::test_agency_request_pr_ambiguous_receipt_reconciles_with_preserved_identity`, `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_update_ambiguous_receipt_reconciles_with_readback`, and `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_reconcile_transient_probe_failure_retries_task`. |
| `expire_approvals` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_worker_background_tasks.py::test_expire_approvals_task_expires_pending_once_and_rearms`. |
| `provider_event_received` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_discord_primary_durable_workflows_acceptance.py::test_google_provider_event_ingress_is_token_bound_deduped_and_conflict_safe`; live positive provider push was not run. |
| `provider_sync_due` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_discord_primary_durable_workflows_acceptance.py::test_google_calendar_sync_persists_provider_evidence_without_ambient_case` and `tests/integration/test_sync_runtime_provider_ingestion.py::test_calendar_sync_refreshes_same_digest_cancelled_evidence`; generic live sync-run rows exist, but a controlled forced sync was not run. Drive change ingestion is explicitly not implemented. |
| `memory_encode` | `passed` | `passed` | Fixture anchors: `tests/integration/test_memory.py::test_memory_remember_enqueues_memory_encode_task` and `tests/integration/test_memory.py::test_memory_remember_enqueues_and_worker_records_encode_turn`; live worker drained a `memory_encode` task. |
| `memory_dream` | `passed` | `not_run` | Fixture anchor: `tests/integration/test_memory.py::test_worker_memory_dream_task_inserts_turn_against_system_session`. |
| `provider_watch_renew_due` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_watch_renew_handler_rearms_near_expiry_channel` and `tests/integration/test_google_provider_ingestion.py::test_worker_provider_watch_renew_due_arm_renews_and_rearms`. |
| `provider_reconcile_sync_due` | `passed` | `not_run` | Fixture anchors: `tests/integration/test_google_provider_ingestion.py::test_reconcile_handler_enqueues_provider_sync_due_for_each_cursor` and `tests/integration/test_google_provider_ingestion.py::test_worker_provider_reconcile_sync_due_arm_enqueues_sync_tasks_and_rearms`; generic sync-run rows do not by themselves prove a current controlled reconcile task. |

### Agency Event Behavior Evidence Ledger

Agency event ingress accepts a closed set of event types. The worker behavior is
event-class-specific: heartbeats do not create jobs, in-progress updates do not
wake, and waiting/terminal events wake the agent.

| Agency event behavior | Contract state | Current-host state | Fixture or live evidence |
| --- | --- | --- | --- |
| heartbeat | `passed` | `not_run` | Fixture anchor: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_heartbeat_marks_processed_without_job_or_wake`. |
| missing-job-id rejection | `passed` | `not_run` | Route validation rejects missing job ids for job events, and fixture anchor `tests/integration/test_worker_background_tasks.py::test_agency_event_received_missing_job_id_fails_event_and_retries_task` proves the worker fails malformed persisted rows without creating jobs. |
| non-waking job update | `passed` | `not_run` | Parameterized fixture anchor covers `job.queued`, `job.started`, and `job.progress`: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_progress_updates_job_without_wake`. |
| waiting-state wake | `passed` | `not_run` | Fixture anchor: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_waiting_wakes_agent_for_approval_state`. |
| terminal-state wake | `passed` | `partial` | Live signed event task completed into job events, but an `agent_wake` row or turn remains unrecorded for the terminal event; parameterized fixture anchor covers `job.completed`, `job.failed`, `job.cancelled`, and `job.timed_out`: `tests/integration/test_worker_background_tasks.py::test_agency_event_received_upserts_job_event_wake_and_deletes_task`. |

## Smoke Sequence

Run these in order and record the result beside each inventory row.

This sequence is the baseline host smoke. It proves service health, auth
boundaries, safe read routes, schema shape, and focused regression tests. It does
not by itself prove every HTTP route, Discord user action, model runtime syscall,
or provider-backed capability in the inventories above.

```sh
.venv/bin/python scripts/verify_production_posture.py --redacted-env-audit --env-file /etc/ariel/ariel.env
.venv/bin/python - <<'PY'
from pathlib import Path

from ariel.config import AppSettings

path = Path(AppSettings().attachment_blob_store_path)
path.mkdir(parents=True, exist_ok=True)
probe = path / ".manual-smoke-write-test"
probe.write_text("ok", encoding="utf-8")
probe.unlink()
print("attachment blob store writable")
PY
```

```sh
systemctl is-active ariel-api ariel-worker ariel-pubsub ariel-discord
systemctl is-enabled ariel-api ariel-worker ariel-pubsub ariel-discord
systemctl show \
  -p ActiveState -p SubState -p FragmentPath -p User -p WorkingDirectory \
  -p EnvironmentFiles -p NoNewPrivileges -p ProtectSystem -p ProtectHome \
  ariel-api ariel-worker ariel-pubsub ariel-discord --no-pager
make production-posture
```

```sh
ss -ltnp
curl -fsS http://127.0.0.1:8000/v1/health \
  | jq '{ok, gmail_pubsub:.subscribers.gmail_pubsub}'
curl -sSI https://ariel.nielseriknandal.com/ | sed -n '1p'
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST 'https://ariel.nielseriknandal.com/v1/providers/google/events?resource_type=calendar&resource_id=primary'
```

```zsh
set -a
if [[ -n "${ARIEL_ENV_FILE:-}" ]]; then
  . "$ARIEL_ENV_FILE"
else
  [[ -f .env ]] && . ./.env
  [[ -f .env.local ]] && . ./.env.local
fi
set +a
auth=( -H "Authorization: Bearer $ARIEL_LOCAL_AUTH_TOKEN" )
curl -sS -o /dev/null -w 'protected memory route status=%{http_code}\n' \
  http://127.0.0.1:8000/v1/memory/log
curl -fsS "${auth[@]}" http://127.0.0.1:8000/ \
  | jq '{ok, surface, api_keys:(.api|keys)}'
sid="$(curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/sessions/active \
  | jq -r '.session.id')"
post_sid="$(curl -fsS "${auth[@]}" -X POST http://127.0.0.1:8000/v1/sessions \
  | jq -r '.session.id')"
[[ "$post_sid" == "$sid" ]] || {
  echo "POST /v1/sessions returned $post_sid, expected $sid"
  exit 1
}
curl -fsS "${auth[@]}" "http://127.0.0.1:8000/v1/sessions/$sid/events" \
  | jq '{ok, session_id, turns:(.turns|length)}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/sessions/rotations?limit=5' \
  | jq '{ok, count:(.rotations|length)}'
curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google \
  | jq '{ok, status:.connector.status, readiness:.connector.readiness, account_identity_present:(.connector.account_email != null), granted_scope_count:(.connector.granted_scopes|length), last_error_code:.connector.last_error_code}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/connectors/google/events?limit=5' \
  | jq '{ok, count:(.events|length)}'
curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google/sync-cursors \
  | jq '{ok, count:(.cursors|length), cursors:[.cursors[] | {resource_type, resource_id, status, has_cursor:(.cursor_value != null), last_error_code}]}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/provider-events?limit=5' \
  | jq '{ok, count:(.events|length)}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/sync-runs?limit=5' \
  | jq '{ok, count:(.sync_runs|length)}'
acct="$(curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google \
  | jq -r '.connector.account_subject // empty')"
[ -n "$acct" ] && curl -fsS -G "${auth[@]}" \
  --data-urlencode "provider_account_id=$acct" \
  --data-urlencode 'limit=5' \
  http://127.0.0.1:8000/v1/email/actions \
  | jq '{ok, count:(.email_actions|length), ids:[.email_actions[].id]}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/discord-messages?limit=5' \
  | jq '{ok, count:(.discord_messages|length), ids:[.discord_messages[].id]}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/jobs?limit=5' \
  | jq '{ok, count:(.jobs|length), ids:[.jobs[].id]}'
.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
import os
import uuid

import httpx
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from ariel.persistence import (
    ActionAttemptRecord,
    ArtifactRecord,
    DiscordMessageEventRecord,
    DiscordMessageRecord,
    JobEventRecord,
    JobRecord,
    ProviderWriteReceiptRecord,
    SessionRecord,
    TurnRecord,
)

database_url = os.environ["ARIEL_DATABASE_URL"]
suffix = uuid.uuid4().hex[:20]
now = datetime.now(UTC)
ids = {
    "session": f"ses_ms_{suffix}",
    "turn": f"trn_ms_{suffix}",
    "email_attempt": f"aat_em_{suffix}",
    "artifact_attempt": f"aat_ar_{suffix}",
    "email_action": f"pwr_ms_{suffix}",
    "discord_message": f"mno_ms_{suffix}",
    "discord_event": f"mne_ms_{suffix}",
    "artifact": f"art_ms_{suffix}",
    "job": f"job_ms_{suffix}",
    "job_event": f"jev_ms_{suffix}",
}
provider_account_id = f"manual_smoke_provider_{suffix}"

engine = create_engine(database_url, future=True)
Session = sessionmaker(bind=engine, future=True)
auth_headers = {}
if token := os.environ.get("ARIEL_LOCAL_AUTH_TOKEN"):
    auth_headers["Authorization"] = f"Bearer {token}"

try:
    with Session.begin() as db:
        db.add(
            SessionRecord(
                id=ids["session"],
                is_active=False,
                lifecycle_state="closed",
                rotated_from_session_id=None,
                rotation_reason=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            TurnRecord(
                id=ids["turn"],
                session_id=ids["session"],
                user_message="manual smoke controlled route detail",
                assistant_message="manual smoke controlled route detail",
                status="completed",
                kind="agent_turn",
                source_background_task_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.add_all(
            [
                ActionAttemptRecord(
                    id=ids["email_attempt"],
                    session_id=ids["session"],
                    turn_id=ids["turn"],
                    proposal_index=1,
                    capability_id="cap.email.archive",
                    capability_version="1.0",
                    capability_contract_hash="a" * 64,
                    impact_level="write_reversible",
                    proposed_input={"message_ids": ["manual-smoke-message"]},
                    payload_hash="b" * 64,
                    policy_decision="requires_approval",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=True,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                ),
                ActionAttemptRecord(
                    id=ids["artifact_attempt"],
                    session_id=ids["session"],
                    turn_id=ids["turn"],
                    proposal_index=2,
                    capability_id="cap.web.extract",
                    capability_version="1.0",
                    capability_contract_hash="c" * 64,
                    impact_level="read",
                    proposed_input={"url": "https://example.com/manual-smoke"},
                    payload_hash="d" * 64,
                    policy_decision="allow_inline",
                    policy_reason=None,
                    status="succeeded",
                    approval_required=False,
                    execution_output={},
                    execution_error=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db.flush()
        db.add(
            ProviderWriteReceiptRecord(
                id=ids["email_action"],
                provider="google",
                provider_account_id=provider_account_id,
                action_attempt_id=ids["email_attempt"],
                capability_id="cap.email.archive",
                idempotency_key=f"manual-smoke:{suffix}",
                status="succeeded",
                provider_object_ids={
                    "message_ids": ["manual-smoke-message"],
                    "thread_ids": ["manual-smoke-thread"],
                },
                request_digest="e" * 64,
                response_payload={"provider_result": {"archived": ["manual-smoke-message"]}},
                ambiguity_reason=None,
                provider_timestamp=None,
                provider_etag=None,
                provider_history_id=None,
                response_digest="f" * 64,
                before_state={
                    "messages": [
                        {"message_id": "manual-smoke-message", "label_ids": ["INBOX"]}
                    ]
                },
                after_state={
                    "messages": [{"message_id": "manual-smoke-message", "label_ids": []}]
                },
                undo_token_hash="g" * 64,
                undo_expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            DiscordMessageRecord(
                id=ids["discord_message"],
                message_id=f"manual-smoke-{suffix}",
                title="Manual smoke Discord message",
                summary="manual smoke controlled route detail",
                source_uri=f"https://discord.com/channels/manual/{suffix}",
                status="active",
                item_metadata={"source": "manual-smoke"},
                observed_at=now,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            DiscordMessageEventRecord(
                id=ids["discord_event"],
                discord_message_id=ids["discord_message"],
                dedupe_key=f"manual-smoke:discord:{suffix}",
                provider_event_id=None,
                event_type="created",
                payload={"message_id": f"manual-smoke-{suffix}", "message": "manual smoke"},
                created_at=now,
            )
        )
        db.add(
            ArtifactRecord(
                id=ids["artifact"],
                session_id=ids["session"],
                turn_id=ids["turn"],
                action_attempt_id=ids["artifact_attempt"],
                artifact_type="retrieval_provenance",
                title="Manual smoke artifact",
                source="https://example.com/manual-smoke",
                snippet="manual smoke route detail",
                retrieved_at=now,
                published_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            JobRecord(
                id=ids["job"],
                source="manual-smoke",
                external_job_id=f"manual-smoke-{suffix}",
                title="Manual smoke Agency job",
                status="succeeded",
                summary="manual smoke controlled route detail",
                latest_payload={"source": "manual-smoke"},
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            JobEventRecord(
                id=ids["job_event"],
                job_id=ids["job"],
                agency_event_id=None,
                event_type="job.completed",
                payload={"source": "manual-smoke"},
                created_at=now,
            )
        )

    with httpx.Client(timeout=10.0, headers=auth_headers) as client:
        email_detail = client.get(
            f"http://127.0.0.1:8000/v1/email/actions/{ids['email_action']}",
            params={"provider_account_id": provider_account_id},
        )
        email_detail.raise_for_status()
        discord_events = client.get(
            f"http://127.0.0.1:8000/v1/discord-messages/{ids['discord_message']}/events"
        )
        discord_events.raise_for_status()
        artifact_detail = client.get(f"http://127.0.0.1:8000/v1/artifacts/{ids['artifact']}")
        artifact_detail.raise_for_status()
        job_detail = client.get(f"http://127.0.0.1:8000/v1/jobs/{ids['job']}")
        job_detail.raise_for_status()
        job_events = client.get(f"http://127.0.0.1:8000/v1/jobs/{ids['job']}/events")
        job_events.raise_for_status()
    print(
        "controlled route detail smoke",
        {
            "email_action_status": email_detail.json()["email_action"]["status"],
            "discord_event_count": len(discord_events.json()["events"]),
            "artifact_type": artifact_detail.json()["artifact"]["type"],
            "job_status": job_detail.json()["job"]["status"],
            "job_event_count": len(job_events.json()["events"]),
        },
    )
finally:
    with Session.begin() as db:
        db.execute(delete(JobEventRecord).where(JobEventRecord.id == ids["job_event"]))
        db.execute(delete(JobRecord).where(JobRecord.id == ids["job"]))
        db.execute(delete(ArtifactRecord).where(ArtifactRecord.id == ids["artifact"]))
        db.execute(
            delete(ProviderWriteReceiptRecord).where(
                ProviderWriteReceiptRecord.id == ids["email_action"]
            )
        )
        db.execute(
            delete(DiscordMessageEventRecord).where(
                DiscordMessageEventRecord.id == ids["discord_event"]
            )
        )
        db.execute(
            delete(DiscordMessageRecord).where(DiscordMessageRecord.id == ids["discord_message"])
        )
        db.execute(
            delete(ActionAttemptRecord).where(
                ActionAttemptRecord.id.in_([ids["email_attempt"], ids["artifact_attempt"]])
            )
        )
        db.execute(delete(TurnRecord).where(TurnRecord.id == ids["turn"]))
        db.execute(delete(SessionRecord).where(SessionRecord.id == ids["session"]))
PY
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/memory/log?limit=5' \
  | jq '{ok, count:(.log|length), items:[.log[] | {id, kind, created_at, taint}]}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/memory/notes?limit=5' \
  | jq '{ok, count:(.notes|length), items:[.notes[] | {id, updated_at}]}'
curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/weather/default-location \
  | jq '{ok, source, updated_at, has_default_location:(.default_location != null)}'
curl -sS -o /dev/null -w 'weather invalid set status=%{http_code}\n' \
  "${auth[@]}" -X PUT -H 'content-type: application/json' \
  -d '{"location":"   "}' \
  http://127.0.0.1:8000/v1/weather/default-location
weather_before="$(curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/weather/default-location)"
previous_location="$(jq -r '.default_location // empty' <<<"$weather_before")"
curl -fsS "${auth[@]}" -X PUT -H 'content-type: application/json' \
  -d '{"location":"San Francisco, CA"}' \
  http://127.0.0.1:8000/v1/weather/default-location \
  | jq '{ok, default_location, source}'
if [[ -n "$previous_location" && "$previous_location" != "San Francisco, CA" ]]; then
  jq -nc --arg location "$previous_location" '{location:$location}' \
    | curl -fsS "${auth[@]}" -X PUT -H 'content-type: application/json' \
      -d @- http://127.0.0.1:8000/v1/weather/default-location \
    | jq '{ok, default_location, source}'
fi
.venv/bin/python - <<'PY'
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
from sqlalchemy import create_engine, text

from ariel.config import AppSettings

base = "http://127.0.0.1:8000/v1/agency/events"
settings = AppSettings()
auth_headers = {}
if settings.local_auth_required:
    if not settings.local_auth_token:
        raise SystemExit("ARIEL_LOCAL_AUTH_REQUIRED is true without ARIEL_LOCAL_AUTH_TOKEN")
    auth_headers["Authorization"] = f"Bearer {settings.local_auth_token}"
secret = os.getenv("ARIEL_AGENCY_EVENT_SECRET")
external_job_id = f"manual-smoke-{uuid.uuid4()}"
title = f"Manual smoke Agency event {external_job_id}"
payload = {
    "source": "manual-smoke",
    "event_id": f"manual-smoke-{uuid.uuid4()}",
    "event_type": "job.completed",
    "external_job_id": external_job_id,
    "title": title,
    "summary": "Signed Agency event smoke.",
    "payload": {"kind": "manual-smoke"},
}
body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
with httpx.Client(timeout=10.0) as client:
    unsigned = client.post(base, content=body, headers={"content-type": "application/json"})
    print(
        "agency unsigned event",
        {"status": unsigned.status_code, "code": unsigned.json().get("error", {}).get("code")},
    )
    if secret:
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
        signed = client.post(
            base,
            content=body,
            headers={
                "content-type": "application/json",
                "X-Ariel-Agency-Timestamp": timestamp,
                "X-Ariel-Agency-Signature": f"sha256={signature.hexdigest()}",
            },
        )
        signed_payload = signed.json()
        print(
            "agency signed event",
            {"status": signed.status_code, "duplicate": signed_payload.get("duplicate")},
        )
        if signed.status_code == 202 and signed_payload.get("duplicate") is False:
            engine = create_engine(str(AppSettings().database_url), future=True)
            deadline = time.time() + 30
            evidence = {}
            while time.time() < deadline:
                with engine.connect() as db:
                    row = db.execute(
                        text(
                            """
                            select
                              ae.status as agency_status,
                              ae.processed_at is not null as processed,
                              j.id as job_id,
                              j.status as job_status,
                              (
                                select count(*)
                                from job_events je
                                where je.agency_event_id = ae.id
                              ) as job_event_count
                              ,
                              (
                                select count(*)
                                from background_tasks bt
                                where bt.task_type = 'agent_wake'
                                  and bt.payload->>'note' like :wake_note_pattern
                              ) as agent_wake_count
                            from agency_events ae
                            left join jobs j
                              on j.source = ae.source
                             and j.external_job_id = ae.external_job_id
                            where ae.source = :source
                              and ae.external_event_id = :event_id
                            limit 1
                            """
                        ),
                        {
                            "source": payload["source"],
                            "event_id": payload["event_id"],
                            "wake_note_pattern": f"%{title}%",
                        },
                    ).mappings().one_or_none()
                evidence = dict(row) if row is not None else {}
                if (
                    evidence.get("processed") is True
                    and evidence.get("job_id")
                    and int(evidence.get("job_event_count") or 0) >= 1
                ):
                    break
                time.sleep(1)
            print("agency worker event", evidence)
            if not (
                evidence.get("processed") is True
                and evidence.get("job_id")
                and int(evidence.get("job_event_count") or 0) >= 1
            ):
                raise SystemExit("agency worker did not process signed event")
            job_id = evidence["job_id"]
            job_detail = client.get(
                f"http://127.0.0.1:8000/v1/jobs/{job_id}",
                headers=auth_headers,
            )
            job_detail.raise_for_status()
            job_events = client.get(
                f"http://127.0.0.1:8000/v1/jobs/{job_id}/events",
                headers=auth_headers,
            )
            job_events.raise_for_status()
            print(
                "agency job routes",
                {
                    "detail_status": job_detail.json()["job"]["status"],
                    "event_count": len(job_events.json()["events"]),
                    "agent_wake_count": evidence.get("agent_wake_count"),
                },
            )
    else:
        print("agency signed event", {"status": "not_enabled"})
PY
curl -sS -o /dev/null -w 'oauth invalid callback status=%{http_code}\n' \
  'https://ariel.nielseriknandal.com/v1/connectors/google/callback?state=manual-smoke-invalid'
curl -fsS "${auth[@]}" -X POST http://127.0.0.1:8000/v1/captures/record \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: manual-smoke-capture' \
  -d '{"kind":"text","text":"manual smoke capture","note":"http api smoke","source":{"app":"manual-smoke"}}' \
  | jq '{ok, capture_id:.capture.id, session_id:.capture.effective_session_id, turn_id:.capture.turn_id}'
```

```sh
.venv/bin/alembic current
.venv/bin/python - <<'PY'
import re

from sqlalchemy import CheckConstraint, create_engine, text

from ariel.config import AppSettings
from ariel.db import schema_readiness_issues
from ariel.persistence import BackgroundTaskRecord


def allowed_background_task_types() -> set[str]:
    for constraint in BackgroundTaskRecord.__table__.constraints:
        if (
            constraint.name == "ck_background_task_type"
            and isinstance(constraint, CheckConstraint)
        ):
            return set(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))
    raise RuntimeError("background task type constraint not found")


allowed = allowed_background_task_types()
engine = create_engine(str(AppSettings().database_url), future=True)
with engine.connect() as db:
    rows = db.execute(
        text("select id, task_type from background_tasks order by created_at limit 500")
    ).all()
    forbidden = [(row.id, row.task_type) for row in rows if row.task_type not in allowed]
    print("readiness", schema_readiness_issues(engine))
    print(
        "task_counts",
        db.execute(
            text(
                "select task_type, count(*), min(run_after), max(attempts) "
                "from background_tasks group by task_type order by task_type"
            )
        ).all(),
    )
    print(
        "provider_reconcile_schedule",
        [
            dict(row)
            for row in db.execute(
                text(
                    """
                    select id, recurrence_seconds, run_after, attempts
                    from background_tasks
                    where task_type = 'provider_reconcile_sync_due'
                    order by created_at desc, id desc
                    limit 5
                    """
                )
            ).mappings()
        ],
    )
    print(
        "overdue_due_rows",
        [
            dict(row)
            for row in db.execute(
                text(
                    """
                    select id, task_type, run_after, attempts
                    from background_tasks
                    where run_after < now() - interval '5 minutes'
                    order by run_after
                    limit 20
                    """
                )
            ).mappings()
        ],
    )
    print(
        "waiting_advisory_locks",
        [
            dict(row)
            for row in db.execute(
                text(
                    """
                    select pid, state, wait_event_type, wait_event, left(query, 160) as query
                    from pg_stat_activity
                    where wait_event_type = 'Lock'
                      and wait_event = 'AdvisoryLock'
                    order by pid
                    """
                )
            ).mappings()
        ],
    )
    print("forbidden_rows", forbidden[:20])
    print(
        "bad_reconcile_shape",
        db.execute(
            text(
                """
                select
                  count(*) filter (
                    where task_type = :task_type
                      and provider_write_receipt_id is null
                  ) as missing_receipt,
                  count(*) filter (
                    where task_type = :task_type
                      and idempotency_key != 'provider_write_reconcile:' || provider_write_receipt_id
                  ) as idempotency_mismatch,
                  count(*) filter (
                    where task_type != :task_type
                      and provider_write_receipt_id is not null
                  ) as unexpected_receipt
                from background_tasks
                """
            ),
            {"task_type": "provider_write_reconcile_due"},
        ).one(),
    )
    print(
        "duplicate_reconcile_receipts",
        db.execute(
            text(
                """
                select provider_write_receipt_id, count(*)
                from background_tasks
                where task_type = :task_type
                  and provider_write_receipt_id is not null
                group by provider_write_receipt_id
                having count(*) > 1
                """
            ),
            {"task_type": "provider_write_reconcile_due"},
        ).all(),
    )
PY
.venv/bin/python -m pytest tests/unit/test_app_config.py
.venv/bin/python -m pytest tests/unit/test_manual_smoke_inventory.py
.venv/bin/python -m pytest tests/unit/test_deploy_artifacts.py
.venv/bin/python -m pytest tests/unit/test_production_posture.py
.venv/bin/python -m pytest tests/unit/test_provider_evidence_lifecycle.py
.venv/bin/python -m pytest tests/unit/test_discord_actions.py tests/unit/test_discord_bot.py
.venv/bin/python -m pytest tests/unit/test_worker_discord_delivery.py
.venv/bin/python -m pytest \
  tests/unit/test_capability_registry_search.py::test_weather_dev_adapter_parses_wttr_payload_without_api_key \
  tests/unit/test_capability_registry_search.py::test_weather_production_adapter_parses_tomorrow_io_payload \
  tests/unit/test_capability_registry_search.py::test_weather_production_adapter_preserves_lat_lon_location_param
.venv/bin/python -m pytest \
  tests/integration/test_google_connector_read_acceptance.py::test_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable \
  tests/integration/test_google_connector_read_acceptance.py::test_email_read_restores_same_digest_provider_evidence \
  tests/integration/test_google_connector_read_acceptance.py::test_calendar_list_same_digest_cancellation_marks_evidence_deleted \
  tests/integration/test_google_provider_ingestion.py::test_connector_sync_cursor_routes_list_cursors_and_enqueue_forced_sync \
  tests/integration/test_google_provider_ingestion.py::test_worker_provider_watch_renew_due_arm_renews_and_rearms \
  tests/integration/test_google_provider_ingestion.py::test_worker_provider_reconcile_sync_due_arm_enqueues_sync_tasks_and_rearms \
  tests/integration/test_sync_runtime_provider_ingestion.py::test_calendar_sync_refreshes_same_digest_cancelled_evidence \
  tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance
.venv/bin/python -m pytest \
  tests/integration/test_session_management_acceptance.py::test_manual_rotation_endpoint_creates_new_active_session_and_is_idempotent
.venv/bin/python -m pytest \
  tests/integration/test_no_ai_ops_acceptance.py::test_capture_record_creates_durable_capture_without_model
.venv/bin/python -m pytest \
  tests/integration/test_main_loop_recovery.py::test_main_loop_emit_done_misuse_recovers_with_typed_nudge
.venv/bin/python -m pytest \
  tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline
.venv/bin/python -m pytest \
  tests/integration/test_memory.py::test_memory_recall_syscall_runs_retriever_inline \
  tests/integration/test_run_program_runtime.py::test_program_reads_a_capability_then_composes_an_emit_message
.venv/bin/python -m pytest tests/integration/test_agency_runtime_capabilities.py
.venv/bin/python -m pytest \
  tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once \
  tests/integration/test_agency_receipt_reconcile.py::test_agency_run_provider_call_started_replay_does_not_call_daemon \
  tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_uses_default_actor_when_actor_id_is_omitted \
  tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_replay_rejects_without_duplicate_execution \
  tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_expired_approval_rejects_without_execution
.venv/bin/python -m pytest tests/integration/test_worker_background_tasks.py
.venv/bin/python -m pytest tests/integration/test_worker_failure_logging.py
.venv/bin/python -m pytest \
  tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority
.venv/bin/python -m pytest \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_action_success_redacts_undo_token_from_event_audit \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_undo_marks_prior_receipt_undone_on_the_single_ledger
.venv/bin/python -m pytest \
  tests/integration/test_search_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations \
  tests/integration/test_search_weather_acceptance.py::test_weather_explicit_location_wins_and_response_contains_location_timeframe_and_timestamps \
  tests/integration/test_search_weather_acceptance.py::test_search_web_egress_fails_closed_before_execute
.venv/bin/python -m pytest \
  tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake
.venv/bin/python -m pytest tests/integration/test_google_connector_readiness_acceptance.py
.venv/bin/python -m pytest tests/integration/test_discord_primary_durable_workflows_acceptance.py
```

Finish current incident iterations with targeted lint, format, type, and
`git diff --check` over changed files. During the fast incident loop, do not use
`make verify` as the inner-loop command; before merge or release, `make verify`
remains the required gate from [codebase.md](codebase.md).

## Recent Evidence Snapshot

This is historical evidence from the 2026-05-24 incident recovery smoke pass,
not an evergreen pass. Treat it as stale until the smoke sequence is rerun on
the current host.

| Area | Historical state | Evidence |
| --- | --- | --- |
| Env parsing | passed | `AppSettings()` now loads the active `.env`/`.env.local` stack with the checked-in provider refs present: OpenRouter for `MAIN`/`RESEARCH`, Google for `VISION`, and OpenAI for `EMBEDDING`. The local redacted audit allows `dev_db` helper keys, but strict production-file audits reject `ARIEL_DB_CONTAINER_NAME`, `ARIEL_DB_DOCKER_IMAGE`, and `ARIEL_DB_VOLUME_NAME` outside local env files. Use `scripts/verify_production_posture.py --redacted-env-audit` for local diagnostics and `--env-file /etc/ariel/ariel.env` for the canonical production file. |
| File permissions | passed | `.env.local`, `.env.dev`, and the GCP service-account JSON are restricted to owner read/write. |
| Database schema | passed | Alembic is at `20260524_0068 (head)` and `schema_readiness_issues()` returns no issues. |
| Background task schema | passed | Live rows are limited to dispatched task types; the schema constraint no longer permits undispatched historical task types. |
| Worker queue liveness | passed | DB-only smoke found no overdue due rows, no waiting advisory-lock backends, no forbidden task types, and no bad or duplicate provider-write reconcile rows. |
| Services | passed | `ariel-api`, `ariel-worker`, `ariel-pubsub`, and `ariel-discord` are active and enabled after restart. |
| Production service posture | failed | `make production-posture` reports all four live Ariel units still run as `niels` from `/home/niels/src/personal/ariel`, without `/etc/ariel/ariel.env` or checked-in systemd hardening; canonical `/opt/ariel`, `/var/lib/ariel`, `/opt/agency`, `/var/lib/agency`, `/usr/local/bin/runsc`, `postgresql.service`, `agency-daemon`, required Pub/Sub env, and production `AppSettings` validation for `/etc/ariel/ariel.env` are also not in canonical posture. |
| Network exposure | passed | API listens on `127.0.0.1:8000`; public Caddy root returns 404; public Google event POST reaches the app and fails validation without provider headers. |
| Health | passed | `/v1/health` returns `ok: true` with a fresh `gmail_pubsub` heartbeat and zero subscriber errors in window. |
| Google connector | passed | Connector is `connected`, readiness is `connected`, account identity is present, and `last_error_code` is null. |
| Google sync cursors | passed | Calendar `primary` cursor is `ready`, has a cursor value, and has no last error. |
| Pub/Sub | passed | Subscriber starts against `projects/ariel-prod-497019/subscriptions/ariel-gmail-watch-sub`; heartbeat remains fresh. |
| Discord bot | passed | Bot token authenticates with Discord REST; configured guild and channel are readable; gateway connects after restart. This proves provider connectivity only; owner/non-owner UI behavior, slash rendering, approval buttons, and attachment-only messages still require Discord UI smokes. |
| OpenRouter main/research models | partial | App-like direct adapter smoke must be refreshed for checked-in `MAIN` (`anthropic/claude-sonnet-4.6`) and `RESEARCH` (`deepseek/deepseek-v3.2`) through OpenRouter. |
| OpenAI embeddings | passed | Live embedding call returned the configured 1536-dimensional numeric vector. |
| Google vision model | passed | Direct Gemini text smoke returned `OK` from checked-in `VISION` (`gemini-2.5-flash`), and a tiny binary-image smoke returned `Red` through the same direct Google ref. Fixture anchor: `tests/integration/test_attachment_content_runtime.py::test_attachment_read_image_and_pdf_use_vision_model_ref` proves image/PDF attachment reads call the checked-in `VISION` ref with binary content and persist provider metadata. |
| Local runtime capabilities | partial | Real gVisor direct run-program smokes passed for `memory.recall`, `memory.remember` with worker-drained `memory_encode`, and memory search/read/note create-edit-delete. `proactive.schedule` and `research.investigate` have fixture-proven syscall-created task drainage through the worker, but worker-drained live completion remains pending. |
| Agency daemon | failed | Canonical system-scope `agency-daemon.service` is absent and `/var/lib/agency/agencyd.sock` does not exist. The current user-service daemon/socket proves only current-host capability binding and does not satisfy production posture. |
| Agency signed events | passed | `ARIEL_AGENCY_EVENT_SECRET` is set; unsigned `POST /v1/agency/events` returns `E_AGENCY_SIGNATURE_MISSING`; a signed manual smoke event is accepted and processed by `ariel-worker`. |
| Provider and connector read smokes | mixed | `search.web`, `web.extract` through Jina Reader, `weather.forecast`, `maps.directions`, `maps.search_places`, `calendar.list`, `calendar.list_calendars`, `email.search`, `email.read`, `drive.search`, and `drive.read` passed live. `calendar.propose_slots` returns a typed success but remains partial for the controlled attendee free/busy check. |
| Verification gate | passed | `make verify` passed on the then-current tree: Ruff, format check, mypy, and 987 tests; one existing Discord `audioop` deprecation warning remains. Rerun after local edits before treating this as current evidence. |

## Recent Follow-Up Queue

These items came from the 2026-05-24 host snapshot. They are not permanent
exceptions, and they are not canonical production requirements. Resolve or
delete them as part of the smoke goal.

- The live Google connector now includes the read, write, Drive, and free/busy
  grants requested during reconnect. Drive search/read now pass through Ariel's
  action runtime. `calendar.propose_slots` still returned a
  primary-calendar-only partial result for the controlled attendee check, so
  all-attendee availability remains the only unresolved Google read caveat.
- The active env now has the checked-in model-provider keys for OpenRouter,
  Google, and OpenAI. Direct app-like smokes passed for `MAIN`, `RESEARCH`,
  and `VISION`. Full main-agent, research-task, Discord UI, and
  attachment-ingress smokes still need to run before this historical incident
  snapshot should be treated as a current end-to-end pass.
- The current systemd units on this host run as `niels`, from
  `/home/niels/src/personal/ariel`, do not use `EnvironmentFile`, and have not
  applied the hardening in `deploy/systemd/*`. [production-runbook.md](production-runbook.md)
  and `deploy/systemd/*` describe a dedicated `ariel` user and
  `/etc/ariel/ariel.env`. `make production-posture` also checks current model
  provider keys in that env file, canonical install roots, `postgresql.service`,
  `runsc`, `agency-daemon`, and `/var/lib/agency/agencyd.sock`; it fails until
  this host is migrated to that canonical shape.
- The current user-service Agency daemon is healthy at
  `/home/niels/.local/share/agency/agencyd.sock`, but it has no tracked smoke
  job and its socket is owned by `niels`. Keep `agency.run`, `agency.status`,
  `agency.artifacts`, and `agency.request_pr` blocked until the canonical
  system daemon is installed or the operator explicitly approves a disposable
  user-service smoke run and PR cleanup.
- Live `attachment.read` is configured but blocked by missing live attachment
  sources. The live DB currently has zero `attachment_sources`,
  `attachment_blobs`, and `attachment_extractions`. Use a real owner Discord
  message with a controlled small attachment; in production `fail_closed`
  scanner mode, verify opaque source persistence and a typed `scan_failed`
  result, not successful extraction.
- `web.extract` provider was cut over from Brave to Jina Reader
  (`https://r.jina.ai`); the bearer key is `ARIEL_JINA_API_KEY`. Existing
  smokes pass.

## Ownership

This document owns the manual smoke checklist and evidence ledger shape. It does
not own provider setup, feature design, or codebase rules.
