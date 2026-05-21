MIN_PY := 12
PYTHON := $(shell for v in .venv/bin/python python3.13 python3.12 python3; do \
  if command -v $$v >/dev/null 2>&1 && $$v -c "import sys; assert sys.version_info >= (3,$(MIN_PY))" 2>/dev/null; then \
    echo $$v; break; \
  fi; \
done)
UV := $(shell if command -v uv >/dev/null 2>&1; then command -v uv; elif [ -x "$$HOME/.local/bin/uv" ]; then echo "$$HOME/.local/bin/uv"; fi)

UVICORN_CMD := .venv/bin/uvicorn ariel.app:create_app --factory --host 127.0.0.1 --port 8000

# Isolated local dev environment. Every dev-* target sets ARIEL_ENV_FILE so the
# app, dev_db helper, and alembic all resolve config from `.env.dev` instead of
# `.env.local` (which systemd reads for prod). Dev defaults to a separate
# container `ariel-postgres-dev` on port 5435 and a separate API on port 8001 —
# see docs/dev-environment.md.
DEV_ENV_FILE := .env.dev
DEV_ENV := ARIEL_ENV_FILE=$(DEV_ENV_FILE)
DEV_UVICORN_CMD := .venv/bin/uvicorn ariel.app:create_app --factory --host 127.0.0.1 --port 8001

.PHONY: help bootstrap setup env-init check-venv check-uv db-up db-stop db-down db-destroy db-status db-logs db-config db-upgrade tailscale-serve run run-worker run-discord dev lint format-check typecheck test verify e2e dev-init dev-up dev-stop dev-down dev-destroy dev-status dev-logs dev-config dev-upgrade dev-api dev-worker dev-discord dev-shell

bootstrap:
	bash scripts/bootstrap.sh

setup: check-uv
	$(UV) sync --locked --extra dev

help:
	@printf "%s\n" \
	  "bootstrap    - one-command first-time setup (prereqs, venv, db)" \
	  "setup        - create .venv and install deps" \
	  "env-init     - create .env.local from .env.example when missing" \
	  "db-up        - start/create local postgres container from ARIEL_DATABASE_URL" \
	  "db-stop      - stop local postgres container" \
	  "db-down      - remove local postgres container (volume preserved)" \
	  "db-destroy   - remove local postgres container and volume" \
	  "db-status    - show local postgres container status" \
	  "db-logs      - show local postgres container logs" \
	  "db-config    - print resolved docker db config from env" \
	  "db-upgrade   - run alembic migrations" \
	  "tailscale-serve - expose app via tailscale (https :443 → localhost:8000)" \
	  "run          - run ariel app" \
	  "run-worker   - run durable background worker" \
	  "run-discord  - run discord surface worker" \
	  "dev          - env-init + db-up + db-upgrade + run API (default env)" \
	  "" \
	  "Isolated dev env (uses .env.dev; does not touch prod systemd or .env.local):" \
	  "dev-init     - create .env.dev from .env.dev.example when missing" \
	  "dev-up       - start/create the dev postgres container (ariel-postgres-dev on :5435)" \
	  "dev-stop     - stop the dev postgres container" \
	  "dev-down     - remove the dev postgres container (volume preserved)" \
	  "dev-destroy  - remove the dev postgres container and volume" \
	  "dev-status   - show the dev postgres container status" \
	  "dev-logs     - show the dev postgres container logs" \
	  "dev-config   - print resolved dev docker db config" \
	  "dev-upgrade  - run alembic migrations against the dev db" \
	  "dev-api      - run ariel API on :8001 against the dev env" \
	  "dev-worker   - run durable worker against the dev env" \
	  "dev-discord  - run discord surface against the dev env" \
	  "dev-shell    - open a subshell with ARIEL_ENV_FILE=.env.dev exported" \
	  "" \
	  "verify       - lint + format check + typecheck + tests" \
	  "e2e          - high-signal end-to-end smoke tests"

env-init:
	@if [ ! -f ".env.local" ]; then \
	  cp .env.example .env.local; \
	  echo "created .env.local from .env.example"; \
	else \
	  echo ".env.local already exists"; \
	fi

check-venv:
	@if [ ! -x ".venv/bin/python" ]; then \
	  echo "missing .venv. run 'make setup' first."; \
	  exit 1; \
	fi

check-uv:
	@if [ -z "$(UV)" ]; then \
	  echo "missing uv. install uv from https://docs.astral.sh/uv/getting-started/installation/"; \
	  exit 1; \
	fi

db-up: env-init
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py up

db-stop:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py stop

db-down:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py down

db-destroy:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py destroy

db-status:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py status

db-logs:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py logs

db-config:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) scripts/dev_db.py print-config

db-upgrade: check-venv
	.venv/bin/alembic upgrade head

tailscale-serve:
	@if command -v tailscale >/dev/null 2>&1; then \
	  tailscale serve --bg --https=443 http://127.0.0.1:8000 && \
	  echo "tailscale serve configured (https :443 → localhost:8000)"; \
	else \
	  echo "tailscale not found. Install from https://tailscale.com/download"; \
	  exit 1; \
	fi

run: check-venv
	$(UVICORN_CMD)

run-worker: check-venv
	.venv/bin/python -m ariel.worker

run-discord: check-venv
	.venv/bin/python -m ariel.discord_bot

dev: db-up check-venv db-upgrade run

lint: check-venv
	.venv/bin/ruff check .

format-check: check-venv
	.venv/bin/ruff format --check .

typecheck: check-venv
	.venv/bin/mypy src tests

test: check-venv
	.venv/bin/python -m pytest

verify: lint format-check typecheck test

e2e: check-venv
	.venv/bin/python -m pytest tests/integration/test_pr01_acceptance.py -k "pr01_turn_context_is_bounded_ordered_and_auditable or pr01_context_audit_is_stable_even_if_adapter_mutates_context_bundle"

# ── Isolated dev env (ARIEL_ENV_FILE=.env.dev) ─────────────────────────
# These targets never read .env.local; they operate against a parallel
# container (ariel-postgres-dev on 127.0.0.1:5435) and a parallel API on
# 127.0.0.1:8001. The prod systemd services keep using .env.local.

dev-init:
	@if [ ! -f "$(DEV_ENV_FILE)" ]; then \
	  cp .env.dev.example $(DEV_ENV_FILE); \
	  echo "created $(DEV_ENV_FILE) from .env.dev.example — edit it and set ARIEL_OPENAI_API_KEY and any dev Discord/connector tokens"; \
	else \
	  echo "$(DEV_ENV_FILE) already exists"; \
	fi

dev-up: dev-init
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py up

dev-stop:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py stop

dev-down:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py down

dev-destroy:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py destroy

dev-status:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py status

dev-logs:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py logs

dev-config:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(DEV_ENV) $(PYTHON) scripts/dev_db.py print-config

dev-upgrade: check-venv
	$(DEV_ENV) .venv/bin/alembic upgrade head

dev-api: check-venv
	$(DEV_ENV) $(DEV_UVICORN_CMD)

dev-worker: check-venv
	$(DEV_ENV) .venv/bin/python -m ariel.worker

dev-discord: check-venv
	$(DEV_ENV) .venv/bin/python -m ariel.discord_bot

dev-shell:
	@echo "starting subshell with ARIEL_ENV_FILE=$(DEV_ENV_FILE) exported (exit to leave)"
	@ARIEL_ENV_FILE=$(DEV_ENV_FILE) $${SHELL:-/bin/bash}
