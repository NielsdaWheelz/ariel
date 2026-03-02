# ariel

slice-0 walking skeleton with:

- fastapi backend
- postgres-backed sessions/turns/events persistence
- single active-session contract
- phone-first web chat surface at `/`
- acceptance-first integration tests using real postgres (testcontainers)

## quickstart

first-time setup (checks prerequisites, creates venv, starts db, runs migrations, configures tailscale):

```bash
make bootstrap
```

if the API key is still the placeholder, edit `.env.local` and re-run `make bootstrap`.

daily development after bootstrap:

```bash
make dev
```

## verification gates

```bash
make verify
# or:
bash scripts/agency_verify.sh
```

## run locally

create local config once (no repeated `export`/`source` needed). app + alembic auto-load `.env.local`.

```bash
make env-init
# edit .env.local with real values for your machine
```

inspect resolved local-db runtime config (derived from `ARIEL_DATABASE_URL`):

```bash
make db-config
```

manage local postgres with docker (idempotent):

```bash
make db-up
make db-status
make db-logs
# when needed:
make db-stop
make db-down
make db-destroy
```

start app in real provider mode:

```bash
make db-upgrade
make run
```

or run end-to-end with one command:

```bash
make dev
```

switch model provider cleanly:

```bash
make run-openai
make run-echo
```

connection-string values (`user/password/database/port`) can be any values you want, as long as:

- they are valid PostgreSQL credentials,
- they match the database actually running,
- and for `make db-up` they point to loopback host (`localhost`, `127.0.0.1`, or `::1`).

explicit shell env vars still override `.env.local` when set.

if migrations are missing, `/v1/*` endpoints return `E_SCHEMA_NOT_READY` (503) until schema is upgraded.

smoke-check the key surfaces:

```bash
curl -sS http://127.0.0.1:8000/v1/health
curl -sS http://127.0.0.1:8000/v1/sessions/active
curl -sS http://127.0.0.1:8000/
```

## private tailnet deployment

for the pr-02 private ingress setup + restart durability verification workflow, use:

- `docs/v1/s0/private_tailnet_runbook.md`
