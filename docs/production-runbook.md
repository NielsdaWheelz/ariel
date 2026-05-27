# Production Runbook

## Scope

Deploy Ariel and Agency on one public DigitalOcean droplet with Discord as the primary
ingress.

Production uses:

- The model refs declared in `src/ariel/models.py`: `MAIN`, `RESEARCH`,
  `VISION`, and `EMBEDDING`.
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

No voice, legacy model override env, public Ariel API, fallback provider, or
Tailscale requirement is part of this deployment.

For running an isolated local dev stack on the same host without touching the
prod database or the running systemd services, see
[dev-environment.md](dev-environment.md). The prod stack defined here reads
`/etc/ariel/ariel.env`; the dev stack reads `.env.dev` via `ARIEL_ENV_FILE`
and runs on parallel ports (Postgres `:5435`, API `:8001`).

## Host Layout

Use a dedicated Linux user:

- User: `ariel`
- App root: `/opt/ariel`
- App state root: `/var/lib/ariel`
- Agency root: `/opt/agency`
- Agency state root: `/var/lib/agency`
- Env file: `/etc/ariel/ariel.env`
- Agency socket: `/var/lib/agency/agencyd.sock`
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
8. Install the Agency binary at `/opt/agency/bin/agency`, owned by `root`
   and executable by the `ariel` service user. The checked-in
   `deploy/systemd/agency-daemon.service` runs this binary directly.

## Cutover From A User Checkout

If the host is still running ad hoc services as a human user from
`/home/<user>/src/personal/ariel`, do not copy that shape into production.
Cut over to the canonical layout first:

1. Commit or otherwise preserve the exact Ariel revision to deploy.
2. From `/opt/ariel`, install the checked-in production service scaffold:

   ```sh
   sudo bash scripts/install_production_services.sh
   ```

   This creates the `ariel` user when missing, prepares root-owned install
   roots under `/opt`, writable runtime state under `/var/lib/ariel` and
   `/var/lib/agency`, installs the checked-in systemd units, and reloads
   systemd. It does not start services.
3. Build `/etc/ariel/ariel.env` from the rotated production secrets and set file
   mode `0640`, owned by `root:ariel`.
4. Install `runsc` on the host `PATH`, install Agency at
   `/opt/agency/bin/agency`, and install Ariel dependencies in `/opt/ariel`.
5. Stop the old user-scoped Ariel and Agency processes only after the canonical
   services are installed and ready to start.
6. Enable the checked-in systemd units, run `make production-posture`, then run
   the Discord and Agency smokes below.

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
ARIEL_GOOGLE_API_KEY=<google-api-key>
ARIEL_OPENROUTER_API_KEY=<openrouter-api-key>
ARIEL_MODEL_TIMEOUT_SECONDS=<seconds>
ARIEL_ATTACHMENT_BLOB_STORE_PATH=/var/lib/ariel/attachment-blobs
```

Required memory settings:

```sh
ARIEL_MEMORY_EMBEDDING_DIMENSIONS=1536
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
and `/capture` are deterministic operational commands only. They expose
rails, state, and operator controls; they do not decide user intent, memory relevance,
run-source choice, or response content. Do not use `ARIEL_DISCORD_CHANNEL_ID` as a
one-channel-only chat gate; it is the default notification and thread parent when a
message-specific Discord target is unavailable.

Required Agency settings:

```sh
ARIEL_AGENCY_SOCKET_PATH=/var/lib/agency/agencyd.sock
ARIEL_AGENCY_ALLOWED_REPO_ROOTS=/opt/ariel,/opt/agency
ARIEL_AGENCY_DEFAULT_BASE_BRANCH=main
ARIEL_AGENCY_DEFAULT_RUNNER=codex
ARIEL_AGENCY_TIMEOUT_SECONDS=30.0
ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS=300
```

Set `ARIEL_AGENCY_EVENT_SECRET=<shared-event-secret>` only when signed
`POST /v1/agency/events` webhook ingress is enabled. When unset, the Agency
daemon socket capabilities still run, but the public Agency event route returns
`E_AGENCY_EVENTS_DISABLED`.

Bootstrap Agency state as the `ariel` service user before enabling the daemon:

```sh
sudo -u ariel env AGENCY_DATA_DIR=/var/lib/agency \
  HOME=/var/lib/agency CODEX_HOME=/var/lib/agency/.codex \
  /opt/agency/bin/agency repo add /opt/ariel
sudo -u ariel env AGENCY_DATA_DIR=/var/lib/agency \
  HOME=/var/lib/agency CODEX_HOME=/var/lib/agency/.codex \
  /opt/agency/bin/agency repo add /opt/agency
sudo -u ariel env AGENCY_DATA_DIR=/var/lib/agency \
  HOME=/var/lib/agency CODEX_HOME=/var/lib/agency/.codex \
  /opt/agency/bin/agency repo ls --json
```

The Agency daemon must have its runner and Codex configuration available under
the same environment used by `agency-daemon.service`. If a user-scoped Agency
daemon was used during recovery, disable it after the system daemon is healthy
and Ariel has been repointed at `/var/lib/agency/agencyd.sock`.

Required worker settings:

```sh
ARIEL_WORKER_POLL_SECONDS=1.0
ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS=3600
```

Required Google Workspace push settings:

```sh
ARIEL_GOOGLE_OAUTH_CLIENT_ID=<google-oauth-client-id>
ARIEL_GOOGLE_OAUTH_CLIENT_SECRET=<google-oauth-client-secret>
ARIEL_GOOGLE_OAUTH_REDIRECT_URI=https://<your-fqdn>/v1/connectors/google/callback
ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS=600
ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS=10
ARIEL_PUBLIC_WEBHOOK_BASE_URL=https://<your-fqdn>
ARIEL_GOOGLE_PUBSUB_TOPIC=projects/<gcp-project>/topics/ariel-gmail-watch
ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION=projects/<gcp-project>/subscriptions/ariel-gmail-watch-sub
ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH=/etc/ariel/secrets/gcp-pubsub-sa.json
```

`ARIEL_PUBLIC_WEBHOOK_BASE_URL` is required in production; Calendar
`events.watch` addresses are derived from it. The OAuth redirect URI is
configured explicitly by `ARIEL_GOOGLE_OAUTH_REDIRECT_URI` and must match the
Google OAuth client. Calendar push callbacks are authenticated by the per-watch
`X-Goog-Channel-Token` values stored with active watch-channel records.
`ARIEL_GOOGLE_PUBSUB_TOPIC`, `ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION`, and
`ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH` are a required group for canonical
production posture. Local/development settings may leave all three unset, but
production posture requires all three.

The single-threaded `ariel-worker` service drains the one `background_tasks`
queue: scheduled agent wakes, provider push and poll ingestion, `memory_encode`,
`memory_dream`, durable action execution, approval expiry, and Agency event
ingestion. There is no separate scheduler process. The worker takes the
earliest due row, dispatches by `task_type`, and on success deletes the row or
re-arms it when it recurs; a failed task backs off within its `attempts` budget
(cap 5). There is no claim protocol, heartbeat, dead-letter state, or stale-task
reaper; a row existing and due is the only pending state.

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
ARIEL_JINA_API_KEY=<jina-api-key>
ARIEL_MAPS_API_KEY=<google-maps-platform-api-key>
ARIEL_WEATHER_PROVIDER_MODE=production
ARIEL_WEATHER_PRODUCTION_API_KEY=<weather-api-key>
```

The model runtime follows the checked-in refs in `src/ariel/models.py`. Keep
provider keys to the current refs: `MAIN` and `RESEARCH` currently require
OpenRouter, `VISION` currently requires Google, and `EMBEDDING` currently
requires OpenAI.

Restrict `ARIEL_MAPS_API_KEY` in the Google Cloud console to the Routes API, Places API
(New), and Geocoding API, and to this deployment's egress IP address. An unrestricted Maps
key is a direct billing liability if it leaks.

There is no leave-by subsystem or configured home origin. A "leave by HH:MM"
reminder is ordinary agent behavior: the agent uses calendar access, the maps
capability, and `proactive.schedule` on a normal wake. See
[modules/proactivity.md](modules/proactivity.md).

## Google Workspace Push

Live Gmail and Calendar push run alongside the reconcile poll. The poll is
the backstop; push is the live path. See
[modules/proactivity.md](modules/proactivity.md) for the standing design.

### Prerequisites

- A public FQDN with an A record to the host's public IPv4; ports 80 and 443
  reachable from the public internet.
- A GCP project with billing enabled; the operator has Owner or Editor and
  `roles/pubsub.admin`.
- `gcloud` installed and authenticated to that project.

### Provision

Run from the repo root:

```sh
export GCP_PROJECT=<your-gcp-project>
bash scripts/gcp_create_runtime_sa.sh
# prints the runtime SA email and the key path
export RUNTIME_SA_EMAIL=ariel-runtime@${GCP_PROJECT}.iam.gserviceaccount.com
bash scripts/gcp_provision_pubsub.sh
# prints the topic and subscription resource paths to paste into the env
```

Move the key into the deployment's secrets dir and chmod 600:

```sh
sudo install -m 0600 -o ariel -g ariel \
  ~/.ariel-secrets/gcp-pubsub-sa.json /etc/ariel/secrets/gcp-pubsub-sa.json
```

### Caddy

```sh
sudo bash deploy/caddy/install.sh
```

The install script provisions Caddy from the official apt repo, deploys
`deploy/caddy/Caddyfile` to `/etc/caddy/Caddyfile`, validates, opens UFW
80/443 if UFW is active, and enables `caddy.service`. The Caddyfile forwards
only `/v1/providers/google/events` and `/v1/connectors/google/callback` to
`127.0.0.1:8000`; every other path returns 404. TLS is provisioned
automatically via Let's Encrypt against the FQDN in the Caddyfile — edit
that file to match your FQDN before running the installer.

### OAuth client

In the GCP console, add
`https://<your-fqdn>/v1/connectors/google/callback` to the OAuth client's
authorized redirect URIs. Without this the connect flow returns
`redirect_uri_mismatch`.

### Smoke test

Connect a Google account from Discord. After consent, the worker registers
a Gmail `users.watch` (publishes to the Pub/Sub topic) and a Calendar
`events.watch` (HTTPS push channel). Verify:

```sh
curl -I https://<your-fqdn>/                                # 404
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST 'https://<your-fqdn>/v1/providers/google/events?resource_type=calendar&resource_id=primary'
# 422 without Google watch headers; provider auth is enforced by the route
curl http://127.0.0.1:8000/v1/health                        # subscribers.gmail_pubsub present
```

Create a Calendar event in the connected account → `provider_events` row
appears within seconds → the worker enqueues `agent_wake` → Ariel posts to
Discord. Send an email to the connected mailbox → same path via Pub/Sub.

### Monitoring

Configure Cloud Monitoring alerts on the subscription:

- `subscription/oldest_unacked_message_age > 5m`
- `subscription/expired_ack_deadlines_count > 1%`
- Any non-zero count of messages on the `ariel-gmail-watch-dlq` topic

A stuck message on the DLQ topic is investigated by pulling from
`ariel-gmail-watch-dlq-sub`. The DLQ is for Pub/Sub delivery exhaustion on
retryable subscriber failures. Malformed payloads, unknown accounts, and inactive
connector accounts are immutable provider data; the subscriber logs and
ack/drops them so they do not churn through retries.

## Services

Run five systemd services:

- `agency-daemon.service`
- `ariel-api.service`
- `ariel-worker.service`
- `ariel-pubsub.service`
- `ariel-discord.service`

The checked-in `agency-daemon.service` starts
`/opt/agency/bin/agency daemon start --foreground`, sets
`AGENCY_DATA_DIR=/var/lib/agency`, and keeps Agency state under
`/var/lib/agency`.

All Ariel services use:

- `User=ariel`
- `Group=ariel`
- `WorkingDirectory=/opt/ariel`
- `EnvironmentFile=/etc/ariel/ariel.env`
- `Restart=always`
- `RestartSec=5`
- `NoNewPrivileges=true`
- `PrivateTmp=true`
- `PrivateDevices=true`
- `ProtectSystem=full`
- `ProtectHome=true`
- `ProtectControlGroups=true`
- `ProtectKernelTunables=true`
- `ProtectKernelModules=true`
- `RestrictSUIDSGID=true`
- `LockPersonality=true`
- `CapabilityBoundingSet=` with no Linux capabilities
- `SystemCallArchitectures=native`

`ariel-api` hosts the in-process `run` sandbox, so its unit must reach `runsc`
on `PATH`. Installing `runsc` to `/usr/local/bin` satisfies this; otherwise add
the install directory to the unit's `PATH`.

Service ordering:

- `ariel-api` starts after Postgres.
- `agency-daemon` starts before Agency-backed Ariel work is accepted.
- `ariel-worker` starts after `ariel-api` and Postgres.
- `ariel-pubsub` starts after `ariel-api` and Postgres.
- `ariel-discord` starts after `ariel-api`.

Start or restart with:

```sh
sudo bash scripts/install_production_services.sh
systemctl daemon-reload
systemctl enable --now agency-daemon ariel-api ariel-worker ariel-pubsub ariel-discord
systemctl restart ariel-api ariel-worker ariel-pubsub ariel-discord
make production-posture
```

`make production-posture` checks systemd state, Ariel unit hardening, the Agency
daemon/socket, loopback health, canonical install/state roots, and
`/etc/ariel/ariel.env` as a production `AppSettings` source. It rejects stale
unknown `ARIEL_*` names, non-production settings, non-canonical Agency roots,
missing required production env vars, non-production `/v1/health` posture,
missing capability-contract digest, and missing provider-evidence surface
health, including current checked-in model provider keys. Add `--check-db-schema` to
`scripts/verify_production_posture.py` only when a direct database readiness
check is needed.

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
8. Start one approval-required `agency.run` smoke task in an allowed repo with
   `no_include_untracked=true`, an explicit runner, and a prompt that can touch
   only a disposable smoke branch or smoke-only file. Verify the resulting job
   with `agency.status` and `agency.artifacts`. Run `agency.request_pr` only
   when the operator has approved the external-send side effect, then close the
   smoke PR unmerged and delete the smoke branch.

## Health Checks

Inspect provider ingestion and the durable timeline through the typed API:

```sh
export ARIEL_LOCAL_AUTH_TOKEN=<local-api-token>
auth=( -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" )
curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google \
  | jq '{ok, status:.connector.status, readiness:.connector.readiness, account_identity_present:(.connector.account_email != null), last_error_code:.connector.last_error_code}'
curl -fsS "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google/sync-cursors \
  | jq '{ok, count:(.cursors|length), cursors:[.cursors[] | {resource_type, resource_id, status, has_cursor:(.cursor_value != null), last_error_code}]}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/provider-events?limit=5' \
  | jq '{ok, count:(.events|length)}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/sync-runs?limit=5' \
  | jq '{ok, count:(.sync_runs|length)}'
curl -fsS "${auth[@]}" 'http://127.0.0.1:8000/v1/discord-messages?limit=5' \
  | jq '{ok, count:(.discord_messages|length), ids:[.discord_messages[].id]}'
```

A proactive wake leaves no proactive-specific record: it is a turn like any
other. Inspect a wake's output and the messages it sent through the timeline
route (`GET /v1/events`); a scheduled wake is an `agent_wake` row on
`background_tasks` until it fires.

Force sync when replaying or diagnosing a specific source. A sync that finds new
data enqueues an `agent_wake` row.

```sh
curl -X POST -H "Authorization: Bearer ${ARIEL_LOCAL_AUTH_TOKEN}" 'http://127.0.0.1:8000/v1/connectors/google/sync?resource_type=calendar&resource_id=primary'
```

System health:

```sh
systemctl is-active postgresql agency-daemon ariel-api ariel-worker ariel-pubsub ariel-discord
journalctl -u ariel-api -u ariel-worker -u ariel-pubsub -u ariel-discord -u agency-daemon --since -15m
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
- No legacy provider override or fallback provider configuration is present.

Functional health:

- Ambient Discord owner DM and home-guild messages receive concise responses unless
  the `run` program finishes silently with `agent.finish_silent`.
- A silent-finish turn records the audited model output and sends no visible assistant
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
systemctl stop ariel-worker ariel-pubsub
```

3. Restore the previous Ariel revision in `/opt/ariel`.
4. Restore the matching database backup when the deployed migration is not backward
   compatible.
5. Restart in dependency order:

```sh
systemctl restart ariel-api
systemctl restart ariel-worker
systemctl restart ariel-pubsub
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

Model providers:

- Confirm the current `MAIN`, `RESEARCH`, `VISION`, and `EMBEDDING` provider keys from
  `src/ariel/models.py` are valid.
- Confirm hosted model calls do not opt into provider-side storage.
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
- Production model paths are limited to the checked-in `MAIN`, `RESEARCH`,
  `VISION`, and `EMBEDDING` refs in `src/ariel/models.py`.
- No legacy provider override, compatibility flag, or fallback provider is configured.
- Every internal capability call goes through validation, policy, egress checks,
  audit logging, and the approval path when required.
- Agency work starts through the `agency.*` run callables and the local daemon socket.
- `ARIEL_AGENCY_ALLOWED_REPO_ROOTS` contains only approved absolute repo roots.
- Postgres survives process restarts with conversations, jobs, approvals, memory, and
  Agency identifiers intact.
- systemd restarts Agency and all four Ariel services after process failure or
  host reboot.
- Caddy exposes only required callback routes over TLS.
- `make verify` passes for the deployed revision, including the required eval groups.
- A Discord smoke test, an approval-button test, and an Agency smoke job pass after
  deploy.
