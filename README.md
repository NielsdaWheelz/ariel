# ariel

slice-0 walking skeleton with:

- fastapi backend
- postgres-backed sessions/turns/events persistence
- single active-session contract
- phone-first web chat surface at `/`
- acceptance-first integration tests using real postgres (testcontainers)

## quickstart

```bash
bash scripts/agency_setup.sh
```

## verification gates

```bash
make verify
# or:
bash scripts/agency_verify.sh
```

## run locally

create a local env file once (no repeated `export`/`source` needed). app + alembic both auto-load `.env.local`.

```bash
cp .env.example .env.local
# edit .env.local with real values for your machine
```

real provider mode (default):

```bash
make db-upgrade
.venv/bin/uvicorn ariel.app:create_app --factory --reload
```

local deterministic dev mode (no external model call):

```bash
ARIEL_MODEL_PROVIDER=echo ARIEL_MODEL_NAME=echo-v1 make db-upgrade
ARIEL_MODEL_PROVIDER=echo ARIEL_MODEL_NAME=echo-v1 .venv/bin/uvicorn ariel.app:create_app --factory --reload
```

env vars still work and take precedence over `.env.local` when explicitly set.

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
