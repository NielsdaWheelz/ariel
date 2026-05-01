# Production Runbook

## Scope

Deploy Ariel and Agency on one public DigitalOcean droplet with Discord as the primary
ingress.

Production uses:

- OpenAI Responses API only.
- Discord Gateway for ambient chat, deterministic slash operations, buttons,
  approvals, jobs, and status.
- Ariel API bound to loopback.
- Agency daemon over a local Unix socket.
- PostgreSQL 16 as canonical storage.
- Caddy-managed TLS for the optional public callback path only.
- systemd for process supervision.

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
ARIEL_BIND_HOST=127.0.0.1
ARIEL_BIND_PORT=8000
ARIEL_OPENAI_API_KEY=<openai-api-key>
ARIEL_MODEL_NAME=gpt-5.5
ARIEL_MODEL_REASONING_EFFORT=medium
ARIEL_MODEL_VERBOSITY=low
ARIEL_MODEL_TIMEOUT_SECONDS=<seconds>
```

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
`/memory`, and `/capture` are deterministic operational commands only. Do not use
`ARIEL_DISCORD_CHANNEL_ID` as a one-channel-only chat gate; it is the default notification
and thread parent when a message-specific Discord target is unavailable.

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
ARIEL_WORKER_HEARTBEAT_TIMEOUT_SECONDS=300
```

Required provider callback settings when Google provider ingress is enabled:

```sh
ARIEL_GOOGLE_PROVIDER_EVENT_TOKEN=<shared-google-callback-token>
```

The same `ariel-worker` service owns provider event sync, workspace signal derivation,
attention review, attention-item follow-ups, approval expiry, Agency event ingestion, and
Discord notification delivery. There is no separate scheduler process.

Set provider keys only for enabled capabilities:

```sh
ARIEL_SEARCH_WEB_API_KEY=<brave-api-key>
ARIEL_SEARCH_NEWS_API_KEY=<optional-news-api-key>
ARIEL_WEB_EXTRACT_API_KEY=<optional-extract-api-key>
ARIEL_MAPS_PROVIDER_API_KEY_ENC=<encrypted-maps-key>
```

Do not set `ARIEL_MODEL_PROVIDER` or `ARIEL_MODEL_API_BASE_URL`.

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
2. Install dependencies with `uv`.
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
8. Start one approval-required `cap.agency.run` smoke task in an allowed repo.

## Health Checks

Inspect proactive state through the typed API:

```sh
curl -s http://127.0.0.1:8000/v1/connectors/google/subscriptions
curl -s http://127.0.0.1:8000/v1/connectors/google/sync-cursors
curl -s http://127.0.0.1:8000/v1/provider-events
curl -s http://127.0.0.1:8000/v1/sync-runs
curl -s http://127.0.0.1:8000/v1/workspace-items
curl -s http://127.0.0.1:8000/v1/attention-signals
curl -s http://127.0.0.1:8000/v1/attention-items
```

Force sync and signal review only through explicit mutation endpoints:

```sh
curl -X POST 'http://127.0.0.1:8000/v1/connectors/google/sync?resource_type=calendar&resource_id=primary'
curl -X POST http://127.0.0.1:8000/v1/attention-signals/derive
```

System health:

```sh
systemctl is-active postgresql agency-daemon ariel-api ariel-worker ariel-discord
journalctl -u ariel-api -u ariel-worker -u ariel-discord -u agency-daemon --since -15m
```

Network health:

```sh
ss -ltnp
curl -fsS http://127.0.0.1:8000/health
```

Expected state:

- Ariel listens only on `127.0.0.1`.
- Discord bot is connected over Gateway.
- Agency socket exists at `ARIEL_AGENCY_SOCKET_PATH`.
- Postgres accepts local connections.
- No Chat Completions or legacy provider configuration is present.

Functional health:

- Ambient Discord owner DM and home-guild messages receive concise responses unless
  the model chooses `cap.discord.no_response`.
- A `cap.discord.no_response` turn records the audited tool output and sends no visible
  assistant text.
- Messages with attachments preserve bounded attachment references in context; raw
  Discord download URLs are not model-visible, and content extraction happens only
  through `cap.attachment.read` with provenance and typed failures.
- Responses function calls create action attempts with audit events.
- Approval-required actions render Discord buttons.
- Duplicate approval clicks do not duplicate side effects.
- `cap.agency.status` can read the smoke Agency job.

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

## Acceptance Criteria

- Ariel API binds to `127.0.0.1` and is not publicly reachable.
- Discord is the production ingress for ambient chat, approvals, jobs, and status
  through one configured home guild plus owner DMs.
- No `/ariel` or `/ask` AI slash commands are registered; `/status`, `/jobs`, `/memory`,
  and `/capture` are deterministic operational commands only.
- Responses API is the only production model path.
- No legacy provider, Chat Completions, compatibility flag, or fallback provider is
  configured.
- Every model tool call goes through capability validation, policy, egress checks, audit
  logging, and the approval path when required.
- Agency work starts through `cap.agency.*` and the local daemon socket.
- `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` contains only approved absolute repo roots.
- Postgres survives process restarts with conversations, jobs, approvals, memory, and
  Agency identifiers intact.
- systemd restarts all four services after process failure or host reboot.
- Caddy exposes only required callback routes over TLS.
- `make verify` passes for the deployed revision, including the required eval groups.
- A Discord smoke test, an approval-button test, and an Agency smoke job pass after
  deploy.
