# Codebase

## Scope

This document covers the tech stack, repository-wide code organization, imports, migrations, and module boundary rules.

## Tech Stack

- Python 3.12+.
- FastAPI (async, factory pattern via `create_app` in `app.py`).
- SQLAlchemy 2.0 ORM + Alembic for migrations.
- Pydantic 2.x for validation; pydantic-settings for config (env prefix `ARIEL_`).
- PostgreSQL 16 for all data storage.
- UV for package management. Not pip, poetry, or conda.
- ruff for linting and formatting (100 char line length).
- mypy in strict mode.
- pytest + testcontainers for testing.
- Docker for the dev database.

## Source Layout

- `src/ariel/` is the main package (setuptools src layout).
- Flat modules. No sub-packages unless unavoidable.
- `tests/unit/` for unit tests. `tests/integration/` for tests that hit a real DB via testcontainers.
- `alembic/` for migration scripts.

## Environment

- Keep `.env.example` in sync with every added, removed, or renamed environment variable.
- Every environment variable read by source code must appear in `.env.example`.
- Each variable in `.env.example` must state whether it is required or optional, and its default if it has one.
- All env vars use the `ARIEL_` prefix and are validated via pydantic-settings in `config.py`.

## Imports

- Every `.py` file starts with `from __future__ import annotations`.
- Relative imports only within `src/ariel/` (e.g., `from .config import Settings`).
- Tests use absolute imports: `from ariel.config import Settings`.
- Do not re-export symbols from other modules. Import each symbol from its defining module.
- No `__init__.py` re-exports beyond what setuptools requires.

## Migrations

- All schema changes go through Alembic. Never modify tables by hand.
- Migration files live in `alembic/versions/`.
- Each migration must be reversible (implement `upgrade` and `downgrade`).

## Module Boundaries

- A module is a single `.py` file under `src/ariel/`.
- Default to internal unless functionality is clearly consumed by other modules.
- `persistence.py` owns all ORM models.
- `db.py` owns engine/session lifecycle.
- `config.py` owns settings and validation.
- `app.py` owns the FastAPI app and request handlers.

## Makefile

- `make bootstrap` — install deps, create dev DB, run migrations.
- `make dev` — start the dev server.
- `make verify` — run ruff + mypy + pytest. All three must pass before merge.

## Style Gates

- ruff and mypy strict are CI gates. No `type: ignore` without a comment explaining why.
- 100 character line limit.
- All functions have type annotations.
