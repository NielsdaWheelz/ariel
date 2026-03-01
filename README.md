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

set `ARIEL_DATABASE_URL`, run migrations, then start uvicorn:

```bash
export ARIEL_DATABASE_URL="postgresql+psycopg://<user>:<password>@localhost/<database>"
make db-upgrade
.venv/bin/uvicorn ariel.app:create_app --factory --reload
```

if migrations are missing, `/v1/*` endpoints return `E_SCHEMA_NOT_READY` (503) until schema is upgraded.

smoke-check the key surfaces:

```bash
curl -sS http://127.0.0.1:8000/v1/health
curl -sS http://127.0.0.1:8000/v1/sessions/active
curl -sS http://127.0.0.1:8000/
```
