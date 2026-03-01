MIN_PY := 12
PYTHON := $(shell for v in python3.13 python3.12 python3; do \
  if command -v $$v >/dev/null 2>&1 && $$v -c "import sys; assert sys.version_info >= (3,$(MIN_PY))" 2>/dev/null; then \
    echo $$v; break; \
  fi; \
done)

.PHONY: setup db-upgrade lint typecheck test verify

setup:
ifndef PYTHON
	$(error No Python >= 3.$(MIN_PY) found. Install Python 3.$(MIN_PY)+ and ensure it is on PATH.)
endif
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

db-upgrade:
	.venv/bin/alembic upgrade head

lint:
	.venv/bin/ruff check .

typecheck:
	.venv/bin/mypy src tests

test:
	.venv/bin/python -m pytest

verify: lint typecheck test
