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
  Discord as the primary ingress, and keeps Responses API as the only model
  path.
- Treat every check as unproven until the command or UI action has been run
  against the current host and recorded.
- Never print secret values. For env checks, print names, presence, provider
  identity, file mode, and API status only.

## Evidence States

- `not_run`: no current evidence.
- `passed`: current evidence proves the item.
- `failed`: current evidence contradicts the expected state.
- `blocked`: the check needs user or provider action before it can run.
- `not_enabled`: the feature is intentionally unset in the active config.

## Env Var Inventory

Put production values in the env file used by the service manager for the host.
The runbook target is `/etc/ariel/ariel.env`; this development server's current
systemd units run from `/home/niels/src/personal/ariel` and load the default
`.env` plus `.env.local` stack through `AppSettings`.

### Core

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_ENV_FILE` | no | Env-file selector | When set, only that env file is loaded; unset uses `.env` plus `.env.local`. |
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
| `ARIEL_OPENAI_API_KEY` | yes | Responses API and embeddings | One low-risk agent turn completes; memory embedding call succeeds when exercised. |
| `ARIEL_MODEL_NAME` | no | Main model | Health/config redacted dump shows intended model. |
| `ARIEL_MODEL_TIMEOUT_SECONDS` | no | Model adapter | Short failure tests remain bounded; normal smoke turn completes. |
| `ARIEL_MODEL_REASONING_EFFORT` | no | Model adapter | Validated as `minimal`, `low`, `medium`, or `high`. |
| `ARIEL_MODEL_VERBOSITY` | no | Model adapter | Validated as `low`, `medium`, or `high`. |
| `ARIEL_MAX_RESPONSE_TOKENS` | no | Agent loop | Overlong output is rejected with bounded failure. |
| `ARIEL_MAIN_TURN_BUDGET_SECONDS` | no | Agent loop | Budget exhaustion test completes gracefully. |
| `ARIEL_RESEARCH_RUN_BUDGET_SECONDS` | no | Research runtime | Research run timeout test or bounded manual research check. |
| `ARIEL_AGENT_LOOP_MAX_MODEL_CALLS` | no | Agent loop | Backstop exhaustion test completes gracefully. |
| `ARIEL_AGENT_LOOP_LIVE_ROUNDS` | no | Agent loop | Prompt-context tests verify live window behavior. |
| `ARIEL_AUTO_ROTATE_MAX_TURNS` | no | Session rotation | Rotation threshold acceptance test or manual forced threshold. |
| `ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS` | no | Session rotation | Rotation age acceptance test or manual forced threshold. |
| `ARIEL_APPROVAL_TTL_SECONDS` | no | Approval runtime | Pending approval expiry test rejects stale approval execution. |
| `ARIEL_APPROVAL_ACTOR_ID` | no | Approval runtime | Approval decisions record the configured default actor when omitted. |

### Memory

| Env var | Secret | Owner | Smoke evidence |
| --- | --- | --- | --- |
| `ARIEL_MEMORY_EMBEDDING_PROVIDER` | no | Memory | Validated nonblank; current production value is `openai`. |
| `ARIEL_MEMORY_EMBEDDING_MODEL` | no | Memory | Validated nonblank. |
| `ARIEL_MEMORY_EMBEDDING_DIMENSIONS` | no | Memory schema | Must match the schema dimension constant. |
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
| `ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH` | yes | Pub/Sub subscriber | Absolute JSON path exists, mode `0600`, and is paired with topic plus subscription. |
| `ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS` | no | Worker sync | Recurring reconcile rows re-arm after successful runs. |
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
| `ARIEL_SEARCH_WEB_API_KEY` | yes | Brave-backed `search.web` and `search.news`; default Brave `web.extract` endpoint | Web and news smokes authenticate with the single Brave key; extract smoke authenticates with this key when no custom extract endpoint is configured. |
| `ARIEL_SEARCH_BRAVE_BASE_URL` | no | Search provider | Nonblank URL; provider call uses configured base. |
| `ARIEL_SEARCH_WEB_TIMEOUT_SECONDS` | no | Search provider | Timeout path is bounded. |
| `ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS` | no | News provider | Timeout path is bounded. |
| `ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT` | no | `web.extract` | Endpoint is HTTPS and extraction smoke succeeds. |
| `ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS` | no | Extract provider | Timeout path is bounded. |
| `ARIEL_WEB_EXTRACT_MAX_RETRIES` | no | Extract provider | Value is between 0 and 5. |
| `ARIEL_MAPS_API_KEY` | yes | Maps | Directions and place search smokes succeed; key is API/IP restricted. |
| `ARIEL_MAPS_TIMEOUT_SECONDS` | no | Maps | Timeout path is bounded. |
| `ARIEL_WEATHER_PROVIDER_MODE` | no | Weather | `production` or `dev`. |
| `ARIEL_WEATHER_PRODUCTION_ENDPOINT` | no | Weather | Tomorrow.io endpoint is nonblank. |
| `ARIEL_WEATHER_PRODUCTION_API_KEY` | yes | Weather | Forecast smoke succeeds in production mode. |
| `ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS` | no | Weather | Timeout path is bounded. |
| `ARIEL_WEATHER_DEV_ENDPOINT` | no | Weather dev | `wttr.in` endpoint is nonblank when dev mode is used. |
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
| `ARIEL_ATTACHMENT_OPENAI_MODEL` | no | Attachment extraction | Text/image extraction uses intended model when enabled. |
| `ARIEL_ATTACHMENT_OPENAI_AUDIO_MODEL` | no | Audio extraction | Audio extraction uses intended model when enabled. |
| `ARIEL_ATTACHMENT_OPENAI_TIMEOUT_SECONDS` | no | Attachment extraction | Extraction timeout path is bounded. |
| `ARIEL_WORKER_POLL_SECONDS` | no | Worker loop | Worker idles and drains due rows at this cadence. |

### Local DB Helper Only

These are parsed only by `src/ariel/dev_db.py` and must not be treated as app
runtime settings:

- `ARIEL_DB_CONTAINER_NAME`
- `ARIEL_DB_DOCKER_IMAGE`
- `ARIEL_DB_VOLUME_NAME`

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
| `GET /v1/connectors/google` | Google connector status | Shows connected identity, readiness, scopes, token state, and last connector error. |
| `GET /v1/connectors/google/events` | Google connector event list | Lists stored connector events. |
| `POST /v1/connectors/google/start` | Start Google connect | Returns OAuth consent URL. |
| `POST /v1/connectors/google/reconnect` | Reconnect Google | Returns OAuth consent URL for replacement credentials. |
| `GET /v1/connectors/google/callback` | Google OAuth callback | Exchanges code, stores encrypted tokens, registers watches. |
| `DELETE /v1/connectors/google` | Disconnect Google | Revokes local connector state and watches. |
| `POST /v1/captures/record` | Capture ingress | Stores a durable capture without enqueuing agent work. |
| `POST /v1/approvals` | Approval decision | Approves/rejects a pending action once; replay is safe but returns not-pending after resolution. |
| `POST /v1/providers/google/events` | Calendar webhook | Authenticates watch token and enqueues provider ingest. |
| `GET /v1/connectors/{provider}/sync-cursors` | Sync cursor read | Lists provider cursor state. |
| `POST /v1/connectors/{provider}/sync` | Force provider sync | Enqueues Gmail, Calendar, or Drive sync and wakes on changes. |
| `GET /v1/provider-events` | Provider event list | Lists raw stored provider event rows. |
| `GET /v1/sync-runs` | Sync run list | Lists recent sync runs. |
| `GET /v1/email/actions` | Email action list | Lists durable email mutations for required `provider_account_id`. |
| `GET /v1/email/actions/{email_action_id}` | Email action detail | Shows one email action and receipts for required `provider_account_id`. |
| `GET /v1/discord-messages` | Discord message list | Shows accepted Discord ingress rows. |
| `GET /v1/discord-messages/{discord_message_id}/events` | Discord timeline | Shows events for one Discord message. |
| `GET /v1/jobs` | Job list | Lists Agency jobs. |
| `GET /v1/jobs/{job_id}` | Job detail | Shows one Agency job. |
| `GET /v1/jobs/{job_id}/events` | Job events | Shows Agency job timeline. |
| `GET /v1/artifacts/{artifact_id}` | Artifact read | Returns stored artifact metadata. |
| `GET /v1/memory/log` | Memory log | Lists memory substrate events. |
| `GET /v1/memory/notes` | Memory notes | Lists operator-visible notes. |

## Discord User Actions

| Action | Expected behavior | Smoke evidence |
| --- | --- | --- |
| Owner DM | Accepted as ambient agent input. | Bot replies or pauses with no duplicate response. |
| Home-guild ambient message | Accepted from the configured owner in the configured guild. | Bot replies in channel or thread. |
| Non-owner message | Ignored in DM and guild contexts. | No API turn is created. |
| Wrong-guild message | Ignored. | No API turn is created. |
| Bot mention | Mention text is stripped before submission. | API turn stores the cleaned user prompt. |
| Attachment-only message | Accepted with the generated attachment prompt. | API turn includes attachment refs and the attachment prompt. |
| `/status` | Deterministic operational status. | Command returns health-ok, active session, and recent job counts. |
| `/jobs` | Deterministic job list. | Command returns recent Agency jobs. |
| `/capture` | Deterministic capture submission. | Capture row appears; capture ingress does not enqueue background work. |
| Wrong-user slash command | Rejected ephemerally. | No API mutation is attempted. |
| Wrong-guild slash command or button | Rejected ephemerally. | No API mutation is attempted. |
| Approval button | Decides pending approval once. | Button-only approval/rejection works; duplicate click does not duplicate side effects. |

### Live Discord Evidence

Run these from the configured owner account and a second non-owner account. Record
message ids, interaction ids, and API row counts only; do not paste private
message content into the smoke log.

1. Send an owner DM with a harmless status request. Confirm one
   `discord_messages` row and one `user_message` background task or completed
   turn.
2. Send an owner message in the home guild, outside the default channel if
   available. Confirm it is accepted and routed back to the origin channel or
   thread.
3. Send the same ambient message from a non-owner account in the home guild and
   by DM. Confirm no API turn or background task is created.
4. Send an ambient message from the owner in a non-home guild. Confirm no API
   turn or background task is created.
5. Mention the bot in the home guild with `<@bot_id> smoke mention strip`.
   Confirm the stored prompt excludes the mention token.
6. Send an attachment-only owner message with a small text fixture. Confirm the
   stored Discord context includes an attachment ref and the prompt is the
   attachment prompt.
7. Run `/status`, `/jobs`, and `/capture` as the owner in the home guild. Confirm
   deterministic ephemeral responses; `/capture` creates a capture row and no
   background task.
8. Run `/status`, `/jobs`, and `/capture` from a non-owner account and in a
   non-home guild. Confirm ephemeral rejection and no API calls.
9. Click one pending approval button from a non-owner account and, if available,
   from a non-home guild copy of the interaction. Confirm ephemeral rejection
   and no API calls.
10. Approve or deny one pending action from its button, then click the same
    button again as the owner in the home guild. Confirm the first click resolves
    the approval and the duplicate click reports not-pending without duplicating
    side effects.

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
- `search.web`, `search.news`, `web.extract`, `maps.*`, and `weather.forecast`
  surface only when their runtime bindings are configured.
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
| `cap.search.news` | `search.news` | Brave search binding configured; bounded query. | News search returns bounded results. |
| `cap.web.extract` | `web.extract` | Extract binding configured; safe public HTTP(S) URL. | Extract a safe public URL. |
| `cap.weather.forecast` | `weather.forecast` | Weather binding configured; explicit location or canonical default. | Forecast returns bounded structured data. |
| `cap.attachment.read` | `attachment.read` | Current turn has attachment refs; scanner/blob policy permits read. | Read a controlled attachment ref. |
| `cap.agency.run` | `agency.run` | Agency runtime configured; socket reachable; approval; allowed repo root. | Starts an approval-required smoke Agency job. |
| `cap.agency.status` | `agency.status` | Agency runtime configured; existing smoke job id. | Reads smoke Agency job state. |
| `cap.agency.artifacts` | `agency.artifacts` | Agency runtime configured; existing smoke job id with artifacts. | Reads smoke Agency job artifacts. |
| `cap.agency.request_pr` | `agency.request_pr` | Agency runtime configured; approval; smoke branch ready for PR. | Approval path requests a PR for a smoke branch. |
| `cap.memory.recall` | `memory.recall` | Memory runtime configured; active turn context. | Retriever returns relevant memory or empty result. |
| `cap.memory.remember` | `memory.remember` | Memory runtime configured; worker drains `memory_encode`. | Enqueues `memory_encode`; worker records a completed encode turn. |
| `cap.memory.search` | `memory.search` | Memory runtime configured; bounded query. | Searches memory substrate. |
| `cap.memory.read` | `memory.read` | Memory runtime configured; existing memory id. | Reads one memory item. |
| `cap.memory.note.create` | `memory.note.create` | Memory runtime configured; controlled note content. | Creates a note. |
| `cap.memory.note.edit` | `memory.note.edit` | Memory runtime configured; existing controlled note id. | Edits that note. |
| `cap.memory.note.delete` | `memory.note.delete` | Memory runtime configured; existing controlled note id. | Deletes that note. |
| `cap.proactive.schedule` | `proactive.schedule` | Active session; RFC3339 wake time; worker drains `agent_wake`. | Schedules one `agent_wake` row. |
| `cap.research.investigate` | `research.investigate` | Research mode selected; mode-specific child capabilities available. | Enqueues and completes a research run. |

### Capability Evidence Ledger

This ledger records current-host live evidence independently from fixture
coverage. `passed` means the capability itself was exercised live in the current
smoke pass. `not_run` means fixture coverage exists or is named, but live
capability evidence has not been collected in this pass.

| Capability | Live state | Fixture or live evidence |
| --- | --- | --- |
| `cap.calendar.list` | `passed` | Live read-only pass returned a bounded event count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.calendar.list_calendars` | `passed` | Live read-only pass returned visible calendar count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.calendar.propose_slots` | `passed` | Live read-only pass returned slots with `availability_scope=all_attendees` and `partial=false`; fixture anchors: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`, `tests/integration/test_google_connector_read_acceptance.py::test_calendar_propose_slots_uses_freebusy_scope_for_all_attendees`, and `tests/integration/test_google_connector_read_acceptance.py::test_attendee_slots_are_limited_scope_and_recoverable_without_freebusy_scope`. |
| `cap.email.search` | `passed` | Live read-only pass returned bounded Gmail message count; fixture anchor: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval`. |
| `cap.email.read` | `passed` | Live read-only pass read one selected message and returned `read_outcome=ok` without printing message content; fixture anchors: `tests/integration/test_google_connector_read_acceptance.py::test_calendar_and_email_read_caps_execute_allowlisted_without_approval` and `tests/integration/test_google_connector_read_acceptance.py::test_email_read_thread_mode_executes_allowlisted_without_approval`. |
| `cap.drive.search` | `not_enabled` | Live connector lacks `drive.metadata.readonly`; fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_search_and_read_execute_inline_with_retrieval_citations`. |
| `cap.drive.read` | `not_enabled` | Live connector lacks `drive.readonly` and no file id came from live `drive.search`; fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_search_and_read_execute_inline_with_retrieval_citations`. |
| `cap.maps.directions` | `blocked` | Live provider returns `provider_permission_denied`; fixture anchor: `tests/integration/test_maps_acceptance.py::test_maps_directions_executes_against_routes_api_with_citations`. |
| `cap.maps.search_places` | `blocked` | Live provider returns `provider_permission_denied`; fixture anchor: `tests/integration/test_maps_acceptance.py::test_maps_search_places_executes_against_places_api_with_metadata`. |
| `cap.calendar.create_event` | `not_enabled` | Live connector lacks `calendar.events`; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_calendar_create_requires_approval_and_executes_exactly_once`. |
| `cap.calendar.update_event` | `not_enabled` | Live connector lacks `calendar.events`; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority`. |
| `cap.calendar.respond_to_event` | `not_enabled` | Live connector lacks `calendar.events`; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority`. |
| `cap.email.draft` | `not_enabled` | Live connector lacks `gmail.compose`; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_email_draft_queues_then_executes_as_draft_only_without_send_side_effect`. |
| `cap.email.send` | `not_enabled` | Live connector lacks `gmail.send`; fixture anchor: `tests/integration/test_google_connector_write_acceptance.py::test_email_send_requires_approval_and_executes_exactly_once`. |
| `cap.email.archive` | `not_enabled` | Live connector lacks `gmail.modify`; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_action_success_redacts_undo_token_from_event_audit`. |
| `cap.email.trash` | `not_enabled` | Live connector lacks `gmail.modify`; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token`. |
| `cap.email.labels.modify` | `not_enabled` | Live connector lacks `gmail.modify`; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token`. |
| `cap.email.undo` | `not_enabled` | Live connector lacks `gmail.modify` and there is no live reversible email mutation receipt from this smoke pass; fixture anchor: `tests/integration/test_email_decluttering_action_runtime.py::test_email_undo_marks_prior_receipt_undone_on_the_single_ledger`. |
| `cap.drive.share` | `not_enabled` | Live connector lacks Drive write scope; fixture anchor: `tests/integration/test_google_drive_capabilities_acceptance.py::test_drive_share_is_approval_gated_exact_payload_and_exactly_once`. |
| `cap.web.extract` | `blocked` | Live provider returns `access_restricted`; fixture anchor: `tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance`. |
| `cap.search.web` | `passed` | Live Brave provider returned bounded web results; fixture anchor: `tests/integration/test_news_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations`. |
| `cap.search.news` | `passed` | Live Brave provider returned bounded news results; fixture anchor: `tests/integration/test_news_weather_acceptance.py::test_news_results_have_sources_citations_and_allowlisted_read_lifecycle`. |
| `cap.weather.forecast` | `passed` | Live Tomorrow.io provider returned a bounded forecast; fixture anchor: `tests/integration/test_news_weather_acceptance.py::test_weather_explicit_location_wins_and_response_contains_location_timeframe_and_timestamps`. |
| `cap.attachment.read` | `blocked` | Capability is configured, but the live DB has zero `attachment_sources`, `attachment_blobs`, or `attachment_extractions`. A real smoke needs an owner Discord message with a controlled small attachment; with current `ARIEL_ATTACHMENT_SCANNER_MODE=fail_closed`, the production expected read result is a typed `scan_failed` outcome unless a clean cached blob or scanner backend exists. Fixture anchor: `tests/integration/test_discord_message_acceptance.py::test_discord_attachment_read_tool_reads_text_attachment`. |
| `cap.agency.run` | `blocked` | Current app settings reach the user-service Agency socket at `/home/niels/.local/share/agency/agencyd.sock`, but canonical system Agency is absent. No live run was started because it would launch a real model runner against the dirty Ariel checkout; run only after canonical `/opt/ariel` posture or an operator-approved disposable smoke repo/branch is available. Fixture anchor: `tests/integration/test_agency_receipt_reconcile.py::test_agency_run_approval_decision_worker_execution_records_job_once`. |
| `cap.agency.status` | `blocked` | Current user-service Agency health is reachable, but no tracked daemon-linked smoke job exists to read. Unblock by completing the controlled `agency.run` smoke first. Fixture anchor: `tests/integration/test_agency_runtime_capabilities.py::test_agency_status_and_artifacts_execute_against_daemon_and_update_job`. |
| `cap.agency.artifacts` | `blocked` | Current user-service Agency health is reachable, but no tracked daemon-linked smoke job with artifacts exists to read. Unblock by completing the controlled `agency.run` smoke first. Fixture anchor: `tests/integration/test_agency_runtime_capabilities.py::test_agency_status_and_artifacts_execute_against_daemon_and_update_job`. |
| `cap.agency.request_pr` | `blocked` | Requires a tracked Agency smoke job and an operator-approved disposable PR side effect. Do not run against the dirty Ariel checkout or merge the smoke PR. Fixture anchor: `tests/integration/test_agency_receipt_reconcile.py::test_agency_request_pr_receipt_ids_are_replayed_without_daemon_call`. |
| `cap.memory.recall` | `passed` | Live direct run-program turn `trn_01ksdaftewxwk0941q3bk0h050` returned `status=recalled` through the real gVisor sandbox and model-backed retriever; fixture anchor: `tests/integration/test_memory.py::test_memory_recall_syscall_runs_retriever_inline`. |
| `cap.memory.remember` | `passed` | Live direct run-program turn `trn_01ksdapjdsysd5jq2any4pcr1j` queued `tsk_01ksdapjn3w8dshr2yca3qaq50`; restarted `ariel-worker` drained it and recorded completed `memory_encode` turn `trn_01ksdapkd5xk7pe3yqemcgac6m`; fixture anchors: `tests/integration/test_memory.py::test_memory_remember_enqueues_memory_encode_task` and `tests/integration/test_memory.py::test_memory_remember_enqueues_and_worker_records_encode_turn`. |
| `cap.memory.search` | `passed` | Live direct run-program turn `trn_01ksd9y83kmrtrjh25a5x760d6` searched for a controlled note marker and returned the note id before cleanup; fixture anchor: `tests/integration/test_run_program_runtime.py::test_program_reads_a_capability_then_composes_an_emit_message` seeds a known memory row and asserts it appears in the syscall result. |
| `cap.memory.read` | `passed` | Live direct run-program turn `trn_01ksd9y83kmrtrjh25a5x760d6` read the controlled note before and after edit; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.create` | `passed` | Live direct run-program turn `trn_01ksd9y83kmrtrjh25a5x760d6` created controlled note `mno_01ksd9y8jfrp9dhsqnsp5ze93j`; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.edit` | `passed` | Live direct run-program turn `trn_01ksd9y83kmrtrjh25a5x760d6` edited controlled note `mno_01ksd9y8jfrp9dhsqnsp5ze93j`; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.memory.note.delete` | `passed` | Live direct run-program turn `trn_01ksd9y83kmrtrjh25a5x760d6` deleted controlled note `mno_01ksd9y8jfrp9dhsqnsp5ze93j`; fixture anchor: `tests/integration/test_normal_turn_program_loop.py::test_memory_note_create_read_delete_syscalls_execute_inline`. |
| `cap.proactive.schedule` | `passed` | Live direct run-program turn `trn_01ksdaqt2xd7y216pgdprqrbjy` created far-future `agent_wake` task `tsk_01ksdaqt6qfrettcnxg3k6dk0f`, then the smoke cleaned it up to avoid an unintended wake; fixture anchor: `tests/integration/test_proactivity_scheduler.py::test_schedule_syscall_writes_an_agent_wake_background_task`. |
| `cap.research.investigate` | `passed` | Live direct run-program returned `status=queued` with a research id in a rolled-back transaction to avoid launching a live research worker job; fixture anchors: `tests/integration/test_research_wiring.py::test_research_investigate_syscall_enqueues_a_research_run_task` and `tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake`. |

### Agency Capability Smoke

Use this only after canonical Agency production posture passes or the operator
has explicitly approved a disposable smoke repo/branch. A healthy user-service
socket is enough to prove current-host capability binding, but it is not enough
to mark production posture passed.

1. Confirm `make production-posture` passes for the canonical
   `agency-daemon.service`, `/var/lib/agency/agencyd.sock`, and allowed repo
   roots. If this host is still using `/home/niels/.local/share/agency`, keep
   the Agency capability rows `blocked`.
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
| `search.web` | `tests/integration/test_news_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations` |
| `search.web` and `search.news` egress | `tests/integration/test_news_weather_acceptance.py::test_search_web_and_news_egress_fails_closed_before_execute` |
| `weather.forecast` dev adapter | `tests/unit/test_capability_registry_search.py::test_weather_dev_adapter_parses_wttr_payload_without_api_key` |
| `weather.forecast` production adapter | `tests/unit/test_capability_registry_search.py::test_weather_production_adapter_parses_tomorrow_io_payload` and `tests/unit/test_capability_registry_search.py::test_weather_production_adapter_preserves_lat_lon_location_param` |
| `maps.directions` | `tests/integration/test_maps_acceptance.py::test_maps_directions_executes_against_routes_api_with_citations` |
| `maps.search_places` | `tests/integration/test_maps_acceptance.py::test_maps_search_places_executes_against_places_api_with_metadata` |
| `web.extract` | `tests/integration/test_web_extract_acceptance.py::test_web_extract_executes_inline_with_structured_output_citations_and_provenance` |

## Model Runtime Syscall Inventory

These syscalls are always host-provided runtime controls, not capabilities.

| Syscall | Smoke evidence |
| --- | --- |
| `agent.emit_message` | Main-agent run can complete with exactly one user-visible message. |
| `agent.emit_value` | Multi-round run can emit bounded internal state. |
| `agent.pause_until_input` | Run can pause without completing a turn. |
| `agent.emit_finding` | Research or retriever run can emit a typed finding; main-agent misuse is rejected. |
| `agent.emit_done` | Rememberer run can end without a user-visible message; main-agent misuse is rejected. |
| `scratch.set` | Program stores a bounded JSON value for the current turn. |
| `scratch.get` | Program reads a scratch value and preserves taint provenance. |

## Background Work Inventory

| Worker path | Expected behavior | Smoke evidence |
| --- | --- | --- |
| `ariel-worker` | Drains `background_tasks`. | Due rows disappear or re-arm; failures back off. |
| Scheduled `agent_wake` | Runs the main agent loop. | Session turn appears and Discord notification is sent when targeted. |
| Provider push ingest | Normalizes Gmail/Calendar signals. | `provider_events`, `sync_runs`, and optional wake are recorded. |
| Provider reconcile poll | Backstop sync for Gmail/Calendar. | Cursor advances and row re-arms. |
| Watch renewal | Re-registers Gmail/Calendar watches before expiry. | Watch expiry moves forward. |
| `memory_encode` | Writes memory assertions/notes. | Memory log records the encoded result. |
| Memory dream | Runs recurring consolidation. | Dream row re-arms after successful run. |
| Durable action execution | Executes approved actions once. | Receipts and action attempts reach a final replay-safe state. |
| Approval expiry | Expires old approvals. | Expired approval cannot execute. |
| Agency event ingest | Reconciles Agency job state. | Job events appear after signed event or status poll. |
| `ariel-pubsub` | Pulls Gmail Pub/Sub notifications. | Fresh subscriber heartbeat; malformed immutable payloads are acked and dropped. |
| `ariel-discord` | Discord gateway ingress, slash commands, and approval buttons. | Gateway connected; slash commands registered. |

## Background Task Type Inventory

Every persisted `background_tasks.task_type` must be accepted by the schema and
dispatched by `ariel-worker`.

| Task type | Expected behavior | Smoke evidence |
| --- | --- | --- |
| `agency_event_received` | Processes signed Agency events into jobs/events and may wake the agent on settled states. | Signed event is accepted; worker removes the task and job event appears. |
| `agent_wake` | Runs the main agent from a note or research-completion wake. | Due wake task completes and creates a turn; targeted wakes notify Discord. |
| `research_run` | Runs bounded research and schedules an agent wake with the finding. | Research task completes, records finding, and enqueues follow-up wake. |
| `user_message` | Runs the main agent for direct or Discord-originated user input. | Message task disappears and the latest turn completes. |
| `execute_action_attempt` | Executes an approved action exactly once. | Approved action reaches a terminal attempt and receipt state. |
| `provider_write_reconcile_due` | Reconciles ambiguous provider-write receipts. | Queued reconcile task deletes on success or retries on transient provider state. |
| `expire_approvals` | Expires pending approvals past their TTL. | Expired approval is marked expired and cannot execute. |
| `provider_event_received` | Processes normalized Google push events. | Provider event moves from accepted to processed or failed. |
| `provider_sync_due` | Runs a Gmail, Calendar, or Drive sync. | Sync run is recorded and cursor state advances or records a typed error. |
| `memory_encode` | Runs the rememberer for an explicit note. | Memory log/note rows record the encoded result. |
| `memory_dream` | Runs recurring memory consolidation. | Recurring task re-arms after the dream run. |
| `provider_watch_renew_due` | Renews expiring Google watches. | Recurring task re-arms; watch expiry moves forward when renewal is due. |
| `provider_reconcile_sync_due` | Enqueues backstop sync tasks for connected Google cursors. | Recurring task re-arms and one-shot `provider_sync_due` rows are enqueued. |

## Smoke Sequence

Run these in order and record the result beside each inventory row.

This sequence is the baseline host smoke. It proves service health, auth
boundaries, safe read routes, schema shape, and focused regression tests. It does
not by itself prove every HTTP route, Discord user action, model runtime syscall,
or provider-backed capability in the inventories above.

```sh
.venv/bin/python - <<'PY'
from ariel.config import AppSettings
AppSettings()
print("settings valid")
PY
.venv/bin/python - <<'PY'
import os
from pathlib import Path

from ariel.config import AppSettings, ENV_FILE_SELECTOR_ENV_VAR
from ariel.dev_db import DEV_DB_ENV_VARS

helper_only = set(DEV_DB_ENV_VARS)
selector_only = {ENV_FILE_SELECTOR_ENV_VAR}

env_files = [Path(os.environ["ARIEL_ENV_FILE"])] if os.environ.get("ARIEL_ENV_FILE") else [
    Path(".env"),
    Path(".env.local"),
]
seen = set()
for env_file in env_files:
    if not env_file.exists():
        continue
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name.removeprefix("export ").strip()
        if name.startswith("ARIEL_"):
            seen.add(name)

runtime_names = {"ARIEL_" + name.upper() for name in AppSettings.model_fields}
unknown = sorted(seen - runtime_names - helper_only - selector_only)
if unknown:
    raise SystemExit(f"unknown ARIEL_* settings: {unknown}")
print("unknown env scanner passed")
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
  | jq '{ok, status:.connector.status, readiness:.connector.readiness, account_identity_present:(.connector.account_email != null), last_error_code:.connector.last_error_code}'
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
[ -n "$acct" ] && email_action_id="$(curl -fsS -G "${auth[@]}" \
  --data-urlencode "provider_account_id=$acct" \
  --data-urlencode 'limit=1' \
  http://127.0.0.1:8000/v1/email/actions \
  | jq -r '.email_actions[0].id // empty')"
[ -n "${email_action_id:-}" ] && curl -fsS -G "${auth[@]}" \
  --data-urlencode "provider_account_id=$acct" \
  "http://127.0.0.1:8000/v1/email/actions/$email_action_id" \
  | jq '{ok, email_action_id:.email_action.id, status:.email_action.status}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/discord-messages?limit=5' \
  | jq '{ok, count:(.discord_messages|length), ids:[.discord_messages[].id]}'
discord_id="$(curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/discord-messages?limit=1' \
  | jq -r '.discord_messages[0].id // empty')"
[ -n "$discord_id" ] && curl -fsS "${auth[@]}" \
  "http://127.0.0.1:8000/v1/discord-messages/$discord_id/events?limit=5" \
  | jq '{ok, count:(.events|length)}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/jobs?limit=5' \
  | jq '{ok, count:(.jobs|length), ids:[.jobs[].id]}'
job_id="$(curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/jobs?limit=1' \
  | jq -r '.jobs[0].id // empty')"
[ -n "$job_id" ] && curl -fsS "${auth[@]}" "http://127.0.0.1:8000/v1/jobs/$job_id" \
  | jq '{ok, job_id:.job.id, status:.job.status}'
[ -n "$job_id" ] && curl -fsS "${auth[@]}" "http://127.0.0.1:8000/v1/jobs/$job_id/events" \
  | jq '{ok, count:(.events|length)}'
artifact_id="$(.venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text

from ariel.config import AppSettings

engine = create_engine(str(AppSettings().database_url), future=True)
with engine.connect() as db:
    row = db.execute(text("select id from artifacts order by created_at desc limit 1")).first()
print(row.id if row is not None else "")
PY
)"
[ -n "$artifact_id" ] && curl -fsS "${auth[@]}" "http://127.0.0.1:8000/v1/artifacts/$artifact_id" \
  | jq '{ok, artifact_id:.artifact.id, source:.artifact.source}'
[ -z "$artifact_id" ] && echo 'artifact route status=not_run'
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
secret = os.getenv("ARIEL_AGENCY_EVENT_SECRET")
payload = {
    "source": "manual-smoke",
    "event_id": f"manual-smoke-{uuid.uuid4()}",
    "event_type": "job.completed",
    "external_job_id": f"manual-smoke-{uuid.uuid4()}",
    "title": "Manual smoke Agency event",
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
.venv/bin/python -m pytest tests/unit/test_discord_bot.py
.venv/bin/python -m pytest tests/unit/test_worker_discord_delivery.py
.venv/bin/python -m pytest \
  tests/unit/test_capability_registry_search.py::test_weather_dev_adapter_parses_wttr_payload_without_api_key \
  tests/unit/test_capability_registry_search.py::test_weather_production_adapter_parses_tomorrow_io_payload \
  tests/unit/test_capability_registry_search.py::test_weather_production_adapter_preserves_lat_lon_location_param
.venv/bin/python -m pytest \
  tests/integration/test_google_connector_read_acceptance.py::test_google_connector_lifecycle_endpoints_are_complete_secure_and_auditable \
  tests/integration/test_google_provider_ingestion.py::test_connector_sync_cursor_routes_list_cursors_and_enqueue_forced_sync \
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
  tests/integration/test_agency_receipt_reconcile.py::test_approval_decision_api_uses_default_actor_when_actor_id_is_omitted
.venv/bin/python -m pytest tests/integration/test_worker_background_tasks.py
.venv/bin/python -m pytest \
  tests/integration/test_email_decluttering_action_runtime.py::test_calendar_write_actions_record_receipts_and_authority
.venv/bin/python -m pytest \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_action_success_redacts_undo_token_from_event_audit \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_mutation_success_records_receipts_and_undo_token \
  tests/integration/test_email_decluttering_action_runtime.py::test_email_undo_marks_prior_receipt_undone_on_the_single_ledger
.venv/bin/python -m pytest \
  tests/integration/test_news_weather_acceptance.py::test_search_web_executes_against_brave_provider_with_citations \
  tests/integration/test_news_weather_acceptance.py::test_news_results_have_sources_citations_and_allowlisted_read_lifecycle \
  tests/integration/test_news_weather_acceptance.py::test_weather_explicit_location_wins_and_response_contains_location_timeframe_and_timestamps \
  tests/integration/test_news_weather_acceptance.py::test_search_web_and_news_egress_fails_closed_before_execute
.venv/bin/python -m pytest \
  tests/integration/test_research_wiring.py::test_worker_research_run_arm_runs_research_and_enqueues_completion_wake
.venv/bin/python -m pytest tests/integration/test_google_connector_readiness_acceptance.py
.venv/bin/python -m pytest tests/integration/test_discord_primary_durable_workflows_acceptance.py
```

Finish with:

```sh
make verify
```

## Recent Evidence Snapshot

This is historical evidence from the 2026-05-24 incident recovery smoke pass,
not an evergreen pass. Treat it as stale until the smoke sequence is rerun on
the current host.

| Area | Historical state | Evidence |
| --- | --- | --- |
| Env parsing | passed | `AppSettings()` loads from the active `.env`/`.env.local` stack without unknown live `ARIEL_*` runtime settings. |
| File permissions | passed | `.env.local`, `.env.dev`, and the GCP service-account JSON are restricted to owner read/write. |
| Database schema | passed | Alembic is at `20260524_0068 (head)` and `schema_readiness_issues()` returns no issues. |
| Background task schema | passed | Live rows are limited to dispatched task types; the schema constraint no longer permits undispatched historical task types. |
| Services | passed | `ariel-api`, `ariel-worker`, `ariel-pubsub`, and `ariel-discord` are active and enabled after restart. |
| Production service posture | failed | `make production-posture` reports all four live Ariel units still run as `niels` from `/home/niels/src/personal/ariel`, without `/etc/ariel/ariel.env` or checked-in systemd hardening; it also checks canonical `agency-daemon` service/socket posture. |
| Network exposure | passed | API listens on `127.0.0.1:8000`; public Caddy root returns 404; public Google event POST reaches the app and fails validation without provider headers. |
| Health | passed | `/v1/health` returns `ok: true` with a fresh `gmail_pubsub` heartbeat and zero subscriber errors in window. |
| Google connector | passed | Connector is `connected`, readiness is `connected`, account identity is present, and `last_error_code` is null. |
| Google sync cursors | passed | Calendar `primary` cursor is `ready`, has a cursor value, and has no last error. |
| Pub/Sub | passed | Subscriber starts against `projects/ariel-prod-497019/subscriptions/ariel-gmail-watch-sub`; heartbeat remains fresh. |
| Discord bot | passed | Bot token authenticates with Discord REST; configured guild and channel are readable; gateway connects after restart. |
| OpenAI Responses | passed | A low-risk live agent turn completed and stored an assistant message. |
| OpenAI embeddings | passed | Live embedding call returned the configured 1536-dimensional numeric vector. |
| Local runtime capabilities | passed | Real gVisor direct run-program smokes passed for `memory.recall`, `memory.remember` with worker-drained `memory_encode`, memory search/read/note create-edit-delete, `proactive.schedule` with cleanup, and `research.investigate` queued-shape in a rolled-back transaction. |
| Agency daemon | failed | Canonical system-scope `agency-daemon.service` is absent and `/var/lib/agency/agencyd.sock` does not exist. The current user-service daemon/socket proves only current-host capability binding and does not satisfy production posture. |
| Agency signed events | passed | `ARIEL_AGENCY_EVENT_SECRET` is set; unsigned `POST /v1/agency/events` returns `E_AGENCY_SIGNATURE_MISSING`; a signed manual smoke event is accepted and processed by `ariel-worker`. |
| Provider direct smokes | mixed | `search.web`, `search.news`, and `weather.forecast` passed live. `maps.*` is blocked by Google API key IP restrictions for the server IPv6 egress address. `web.extract` is blocked by provider 403 from Brave extract. |
| Verification gate | passed | `make verify` passed on the final tree: Ruff, format check, mypy, and 956 tests; one existing Discord `audioop` deprecation warning remains. |

## Recent Follow-Up Queue

These items came from the 2026-05-24 host snapshot. They are not permanent
exceptions, and they are not canonical production requirements. Resolve or
delete them as part of the smoke goal.

- `docs/rules/*` does not exist in the current tree. The canonical repo-wide
  standards are the flat docs linked from [docs/index.md](index.md), especially
  [cleanliness.md](cleanliness.md).
- The current systemd units on this host run as `niels`, from
  `/home/niels/src/personal/ariel`, do not use `EnvironmentFile`, and have not
  applied the hardening in `deploy/systemd/*`. [production-runbook.md](production-runbook.md)
  and `deploy/systemd/*` describe a dedicated `ariel` user and
  `/etc/ariel/ariel.env`. `make production-posture` also checks canonical
  `agency-daemon` service posture and `/var/lib/agency/agencyd.sock`; it fails
  until this host is migrated to that canonical shape.
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
- Live `maps.directions` and `maps.search_places` currently return
  `provider_permission_denied` because Google reports
  `API_KEY_IP_ADDRESS_BLOCKED` for egress IP `2a01:4ff:1f0:241e::1`. Update the
  `ARIEL_MAPS_API_KEY` application restriction to include that IPv6 egress
  address, or route provider calls through an allowed egress address.
- Live `web.extract` currently returns `access_restricted`; Brave extract
  responds `403 Forbidden` from `api.search.brave.com` even though `search.web`
  and `search.news` succeed with the same Brave key. Check Brave plan/feature
  access for the Web Extract endpoint or configure a supported
  `ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT`.

## Ownership

This document owns the manual smoke checklist and evidence ledger shape. It does
not own provider setup, feature design, or codebase rules.
