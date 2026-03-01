.PHONY: setup db-upgrade lint typecheck test verify

setup:
	python3 -m venv .venv
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
