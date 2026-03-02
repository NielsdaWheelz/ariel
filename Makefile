MIN_PY := 12
PYTHON := $(shell for v in python3.13 python3.12 python3; do \
  if command -v $$v >/dev/null 2>&1 && $$v -c "import sys; assert sys.version_info >= (3,$(MIN_PY))" 2>/dev/null; then \
    echo $$v; break; \
  fi; \
done)

UVICORN_CMD := .venv/bin/uvicorn ariel.app:create_app --factory --host 127.0.0.1 --port 8000

.PHONY: help setup env-init check-venv db-up db-stop db-down db-destroy db-status db-logs db-config db-upgrade run run-openai run-echo dev lint typecheck test verify

setup:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

help:
	@printf "%s\n" \
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
	  "run          - run ariel app (provider from .env.local/env)" \
	  "run-openai   - run app forcing openai provider" \
	  "run-echo     - run app forcing echo provider" \
	  "dev          - env-init + db-up + db-upgrade + run" \
	  "verify       - lint + typecheck + tests"

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

run: check-venv
	$(UVICORN_CMD)

run-openai: check-venv
	ARIEL_MODEL_PROVIDER=openai $(UVICORN_CMD)

run-echo: check-venv
	ARIEL_MODEL_PROVIDER=echo ARIEL_MODEL_NAME=echo-v1 $(UVICORN_CMD)

dev: db-up check-venv db-upgrade run

lint: check-venv
	.venv/bin/ruff check .

typecheck: check-venv
	.venv/bin/mypy src tests

test: check-venv
	.venv/bin/python -m pytest

verify: lint typecheck test
