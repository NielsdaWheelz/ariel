from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "alembic_version",
    "sessions",
    "turns",
    "events",
    "action_attempts",
    "approval_requests",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    project_root = _project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations(database_url: str, *, revision: str = "head") -> None:
    command.upgrade(_alembic_config(database_url), revision)


def reset_schema_for_tests(engine: Engine, database_url: str) -> None:
    if engine.dialect.name != "postgresql":
        msg = "test schema reset only supports postgresql"
        raise RuntimeError(msg)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(database_url)


def missing_required_tables(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    return [table_name for table_name in REQUIRED_TABLES if not inspector.has_table(table_name)]
