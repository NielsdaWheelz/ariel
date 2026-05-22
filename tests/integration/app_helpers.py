from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine

from ariel.app import create_app
from tests.db_helpers import reset_postgres_schema


def create_migrated_app(
    *,
    database_url: str,
    model_adapter: Any | None = None,
    sandbox: Any | None = None,
) -> Any:
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        reset_postgres_schema(engine, database_url)
    finally:
        engine.dispose()
    return create_app(
        database_url=database_url,
        model_adapter=model_adapter,
        sandbox=sandbox,
    )
