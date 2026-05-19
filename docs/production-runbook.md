# Production Runbook

## Scope

Deploy Ariel and Agency on one public DigitalOcean droplet with Discord as the primary
ingress.

Production uses:

- OpenAI Responses API only.
- Discord Gateway for ambient chat, operational slash commands, buttons,
  approvals, jobs, and status. Slash commands are rails and control surfaces,
  not AI judgment surfaces.
- Ariel API bound to loopback.
- Agency daemon over a local Unix socket.
- PostgreSQL 16 as canonical storage.
- Caddy-managed TLS for the optional public callback path only.
- systemd for process supervision.

Production follows [ai-first.md](ai-first.md): model and subagent calls own
judgment; deterministic services own validation, authorization, idempotency,
taint, replay, recovery, and audit.

No voice, legacy model provider, Chat Completions, public Ariel API, fallback provider, or
Tailscale requirement is part of this deployment.

## Host Layout

Use a dedicated Linux user:

- User: `ariel`
- App root: `/opt/ariel`
- Agency root: `/opt/agency`
- Env file: `/etc/ariel/ariel.env`
- Agency socket: `/run/agency/agency-daemon.sock`
- Ariel API bind: `127.0.0.1:8000`
- Postgres database: `ariel`

Allowed Agency repositories must be absolute, symlink-resolved paths under the approved
repo roots.

## Droplet Baseline

1. Create a current Ubuntu LTS droplet.
2. Allow inbound SSH only from approved source addresses.
3. Allow inbound `80/tcp` and `443/tcp` only when Caddy serves a required public callback.
4. Install system packages:

```sh
apt-get update
apt-get install -y caddy git postgresql postgresql-contrib
```

5. Install `uv` for the `ariel` user.
6. Install Agency using its production installation path.
7. Clone Ariel into `/opt/ariel` and Agency into `/opt/agency`.

## Sandbox Runtime

The `run` tool executes each model-authored Python program inside a gVisor
(`runsc`) sandbox. The sandbox runs in-process inside the `ariel-api` service —
`SandboxRuntime` is started and stopped in the FastAPI lifespan. There is no
separate systemd service. `ariel-api` therefore needs `runsc` reachable on its
`PATH`.

Install the `runsc` release binary onto the host `PATH`:

```sh
curl -fsSLO https://storage.googleapis.com/gvisor/releases/release/latest/$(uname -m)/runsc
chmod 0755 runsc
install -m 0755 runsc /usr/local/bin/runsc
```

`runsc` runs rootless and uses the Systrap platform, which needs no KVM. It
requires a kernel with unprivileged user namespaces enabled. On Ubuntu 24.04+
the AppArmor restriction `kernel.apparmor_restrict_unprivileged_userns` must be
`0` for rootless `runsc` to launch a sandbox. Persist it with a sysctl drop-in:

```sh
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' \
  > /etc/sysctl.d/60-ariel-runsc.conf
sysctl --system
```

Confirm the sandbox can launch a container as the `ariel` user:

```sh
sudo -u ariel runsc --rootless --network=none do true && echo runsc ok
```

## Postgres

Create the production role and database:

```sh
sudo -u postgres createuser --pwprompt ariel
sudo -u postgres createdb --owner ariel ariel
```

Set `ARIEL_DATABASE_URL` to the local Postgres URL:

```sh
ARIEL_DATABASE_URL=postgresql+psycopg://ariel:<password>@127.0.0.1:5432/ariel
```

Run Alembic migrations from `/opt/ariel` before starting services.

## Environment

Store production configuration in `/etc/ariel/ariel.env`, owned by root and readable by
the service user.

Required core settings:

```sh
ARIEL_DATABASE_URL=postgresql+psycopg://ariel:<password>@127.0.0.1:5432/ariel
ARIEL_DEPLOYMENT_MODE=production
ARIEL_BIND_HOST=127.0.0.1
ARIEL_BIND_PORT=8000
ARIEL_LOCAL_AUTH_REQUIRED=true
ARIEL_LOCAL_AUTH_TOKEN=<32-plus-char-url-safe-random-token>
ARIEL_CONNECTOR_ENCRYPTION_SECRET=<non-dev-connector-secret>
ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION=v1
ARIEL_CONNECTOR_ENCRYPTION_KEYS='{"v1":"<base64url-16-24-or-32-byte-key>"}'
ARIEL_OPENAI_API_KEY=<openai-api-key>
ARIEL_MODEL_NAME=gpt-5.5
ARIEL_MODEL_REASONING_EFFORT=medium
ARIEL_MODEL_VERBOSITY=low
ARIEL_MODEL_TIMEOUT_SECONDS=<seconds>
```

Required memory settings:

```sh
ARIEL_MEMORY_EMBEDDING_PROVIDER=openai
ARIEL_MEMORY_EMBEDDING_MODEL=text-embedding-3-small
ARIEL_MEMORY_EMBEDDING_DIMENSIONS=1536
ARIEL_MEMORY_IMPORT_CUTOVER_ENABLED=false
```

Keep `ARIEL_MEMORY_IMPORT_CUTOVER_ENABLED=false` outside an explicit one-time
cutover window. Routine memory creation, correction, deletion, scope policy, and
consolidation are handled through audited AI memory capabilities.

Required Discord settings:

```sh
ARIEL_DISCORD_BOT_TOKEN=<discord-bot-token>
ARIEL_DISCORD_GUILD_ID=<guild-id>
ARIEL_DISCORD_CHANNEL_ID=<default-notification-channel-id>
ARIEL_DISCORD_USER_ID=<owner-user-id>
ARIEL_DISCORD_ARIEL_BASE_URL=http://127.0.0.1:8000
ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS=10.0
```

`ARIEL_DISCORD_GUILD_ID` is the one home guild. Owner DMs are also accepted. Ambient
messages are the Discord AI surface; `/ariel` and `/ask` are gone. `/status`, `/jobs`,
and `/capture` are deterministic operational commands only. They expose
rails, state, and operator controls; they do not decide user intent, memory relevance,
run-source choice, or response content. Do not use `ARIEL_DISCORD_CHANNEL_ID` as a
one-channel-only chat gate; it is the default notification and thread parent when a
message-specific Discord target is unavailable.

Required Agency settings:

```sh
ARIEL_AGENCY_SOCKET_PATH=/run/agency/agency-daemon.sock
ARIEL_AGENCY_ALLOWED_REPO_ROOTS=/opt/ariel,/opt/agency
ARIEL_AGENCY_DEFAULT_BASE_BRANCH=main
ARIEL_AGENCY_DEFAULT_RUNNER=codex
ARIEL_AGENCY_TIMEOUT_SECONDS=30.0
ARIEL_AGENCY_EVENT_SECRET=<shared-event-secret>
ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS=300
```

Required worker settings:

```sh
ARIEL_WORKER_POLL_SECONDS=1.0
ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS=3600
```

Required provider callback settings when Google provider ingress is enabled:

```sh
ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN=<shared-google-callback-token>
```

The single-threaded `ariel-worker` service drains the one `background_tasks`
queue: scheduled agent wakes, provider push and poll ingestion, the memory
rememberer and sweep, durable action execution, approval expiry, and Agency
event ingestion. There is no separate scheduler process. The worker takes the
earliest due row, dispatches by `task_type`, and on success deletes the row or
re-arms it when it recurs; a failed task backs off within its `attempts` budget
(cap 5). There is no claim protocol, heartbeat, dead-letter state, or stale-task
reaper — a row existing and due is the only pending state.

Proactivity is not a separate engine. A provider push, a poll result that finds
new data, a due scheduled task, and a Google connector error each enqueue an
`agent_wake` row; the worker dispatches it to the same agent loop that serves a
user message. `ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS` (default 3600)
sets the reconcile-poll cadence, the push-independent baseline. The worker
re-arms each Gmail and Calendar `watch` before it expires. See
[modules/proactivity.md](modules/proactivity.md).

Set provider keys only for enabled capabilities:

```sh
ARIEL_SEARCH_WEB_API_KEY=<brave-api-key>
ARIEL_SEARCH_NEWS_API_KEY=<optional-news-api-key>
ARIEL_WEB_EXTRACT_API_KEY=<optional-extract-api-key>
ARIEL_MAPS_API_KEY=<google-maps-platform-api-key>
ARIEL_WEATHER_PROVIDER_MODE=production
ARIEL_WEATHER_PRODUCTION_API_KEY=<weather-api-key>
```

Do not set `ARIEL_MODEL_PROVIDER` or `ARIEL_MODEL_API_BASE_URL`.

Restrict `ARIEL_MAPS_API_KEY` in the Google Cloud console to the Routes API, Places API
(New), and Geocoding API, and to this deployment's egress IP address. An unrestricted Maps
key is a direct billing liability if it leaks.

Optional home-address setting:

```sh
ARIEL_HOME_ADDRESS=<street address>
```

`ARIEL_HOME_ADDRESS` is an optional maps origin fallback used when no preceding
located calendar event resolves a trip origin; with it unset, those trips are
skipped. There is no leave-by subsystem: a "leave by HH:MM" reminder is now an
ordinary agent behavior — the agent uses calendar access, the maps capability,
and `proactive.schedule` on a normal wake. See
[modules/proactivity.md](modules/proactivity.md).

## Services

Run four systemd services:

- `agency-daemon.service`
- `ariel-api.service`
- `ariel-worker.service`
- `ariel-discord.service`

All Ariel services use:

- `User=ariel`
- `WorkingDirectory=/opt/ariel`
- `EnvironmentFile=/etc/ariel/ariel.env`
- `Restart=always`
- `RestartSec=5`

`ariel-api` hosts the in-process `run` sandbox, so its unit must reach `runsc`
on `PATH`. Installing `runsc` to `/usr/local/bin` satisfies this; otherwise add
the install directory to the unit's `PATH`.

Service ordering:

- `ariel-api` starts after Postgres.
- `agency-daemon` starts before Agency-backed Ariel work is accepted.
- `ariel-worker` starts after `ariel-api` and Postgres.
- `ariel-discord` starts after `ariel-api`.

Start or restart with:

```sh
systemctl daemon-reload
systemctl enable --now agency-daemon ariel-api ariel-worker ariel-discord
systemctl restart ariel-api ariel-worker ariel-discord
```

## Caddy And TLS

Ariel does not expose a public API. Keep `ARIEL_BIND_HOST=127.0.0.1`.

Configure Caddy only for required public HTTPS callbacks. Forward the narrow callback
path to `127.0.0.1:8000`; do not proxy generic Ariel routes.

Check TLS and routing:

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

## Deployment

1. Pull the intended Ariel revision into `/opt/ariel`.
2. Install locked runtime and verification dependencies with `make setup`
   (`uv sync --locked --extra dev`).
3. Run verification before replacing services. `make verify` includes the required pytest
   eval suite for Responses routing, policy, Agency, Discord, worker recovery, and memory:

```sh
make verify
```

4. Run database migrations.
5. Restart services.
6. Confirm health checks.
7. Send one ambient owner DM smoke message and one ambient owner home-guild smoke
   message.
8. Start one approval-required `agency.run` smoke task in an allowed repo.

## Health Checks

Inspect provider ingestion and the durable timeline through the typed API:

```sh
export ARIEL_LOCAL_AUTH_TOKEN=<local-api-token>
curl -s -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" http://127.0.0.1:8000/v1/connectors/google
curl -s -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" http://127.0.0.1:8000/v1/connectors/google/sync-cursors
curl -s -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" http://127.0.0.1:8000/v1/provider-events
curl -s -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" http://127.0.0.1:8000/v1/sync-runs
curl -s -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" http://127.0.0.1:8000/v1/discord-messages
```

A proactive wake leaves no proactive-specific record: it is a session turn like
any other. Inspect a wake's output and the messages it sent through the normal
session and timeline routes (`/v1/sessions/{session_id}/events`); a scheduled
wake is an `agent_wake` row on `background_tasks` until it fires.

Force sync when replaying or diagnosing a specific source. A sync that finds new
data enqueues an `agent_wake` row.

```sh
curl -X POST -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" 'http://127.0.0.1:8000/v1/connectors/google/sync?resource_type=calendar&resource_id=primary'
```

System health:

```sh
systemctl is-active postgresql agency-daemon ariel-api ariel-worker ariel-discord
journalctl -u ariel-api -u ariel-worker -u ariel-discord -u agency-daemon --since -15m
```

Network health:

```sh
ss -ltnp
curl -fsS http://127.0.0.1:8000/v1/health
```

Expected state:

- Ariel listens only on `127.0.0.1`.
- Discord bot is connected over Gateway.
- Agency socket exists at `ARIEL_AGENCY_SOCKET_PATH`.
- Postgres accepts local connections.
- No Chat Completions or legacy provider configuration is present.

Functional health:

- Ambient Discord owner DM and home-guild messages receive concise responses unless
  the `run` program pauses with `agent.pause_until_input`.
- A pause turn records the audited model output and sends no visible assistant
  text.
- Messages with attachments preserve bounded attachment references in context; raw
  Discord download URLs are not model-visible, and content extraction happens only
  through the `attachment.read` callable with provenance and typed failures.
- Internal `run` capability calls create action attempts with audit events.
- Approval-required actions render Discord buttons.
- Duplicate approval clicks do not duplicate side effects.
- `agency.status` can read the smoke Agency job.

## Rollback

Rollback is a production recovery action, not a compatibility mode.

1. Stop Discord ingress first:

```sh
systemctl stop ariel-discord
```

2. Stop workers if they are executing unsafe or unwanted work:

```sh
systemctl stop ariel-worker
```

3. Restore the previous Ariel revision in `/opt/ariel`.
4. Restore the matching database backup when the deployed migration is not backward
   compatible.
5. Restart in dependency order:

```sh
systemctl restart ariel-api
systemctl restart ariel-worker
systemctl restart ariel-discord
```

6. Re-run health checks and the Discord smoke test.

Do not re-enable removed legacy model paths or fallback providers during rollback.

## Recovery

Postgres:

- Restore from the latest verified backup.
- Run migrations only after confirming the restored revision.
- Confirm conversations, approvals, jobs, artifacts, memory, and Agency state are present.

Agency:

- Restart `agency-daemon`.
- Confirm the socket path exists and is owned for Ariel access.
- Reconcile Ariel jobs against Agency task and invocation ids before retrying work.

Discord:

- Restart `ariel-discord`.
- Confirm the bot reconnects to Gateway.
- Confirm ambient owner DMs and configured home-guild messages are accepted.
- Re-issue status messages for active jobs when needed.

OpenAI:

- Confirm `ARIEL_OPENAI_API_KEY` is valid.
- Confirm Responses calls use `store: false`.
- Do not persist raw reasoning items during incident capture.

Worker:

- Confirm `ariel-worker` is running before treating provider ingestion and
  scheduled wakes as healthy.
- A failed `background_tasks` row stays in place with `attempts` incremented and
  `run_after` pushed out for backoff; the worker retries it on a later pass.
- On `attempts` exhaustion (cap 5) a one-shot row is deleted and a recurring row
  is re-armed to its next occurrence. There is no dead-letter state and no
  reaper; an operator stops a task by deleting its row.

## Acceptance Criteria

- Ariel API binds to `127.0.0.1` and is not publicly reachable.
- Discord is the production ingress for ambient chat, approvals, jobs, and status
  through one configured home guild plus owner DMs.
- No `/ariel` or `/ask` AI slash commands are registered; `/status`, `/jobs`, and
  `/capture` are deterministic operational rails only.
- Responses API is the only production model path.
- No legacy provider, Chat Completions, compatibility flag, or fallback provider is
  configured.
- Every internal capability call goes through validation, policy, egress checks,
  audit logging, and the approval path when required.
- Agency work starts through the `agency.*` run callables and the local daemon socket.
- `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` contains only approved absolute repo roots.
- Postgres survives process restarts with conversations, jobs, approvals, memory, and
  Agency identifiers intact.
- systemd restarts all four services after process failure or host reboot.
- Caddy exposes only required callback routes over TLS.
- `make verify` passes for the deployed revision, including the required eval groups.
- A Discord smoke test, an approval-button test, and an Agency smoke job pass after
  deploy.
