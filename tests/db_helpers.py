from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ariel.db import run_migrations


def reset_postgres_schema(engine: Engine, database_url: str) -> None:
    if engine.dialect.name != "postgresql":
        msg = "test schema reset only supports postgresql"
        raise RuntimeError(msg)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    run_migrations(database_url)
