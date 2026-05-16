from __future__ import annotations

from collections.abc import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests


@pytest.fixture(autouse=True)
def _hermetic_app_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic against a host .env/.env.local: every AppSettings()
    resolves from env vars and code defaults only, never from a developer's env files."""
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)


@pytest.fixture(scope="session")
def postgres_container_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        yield postgres.get_connection_url().replace("psycopg2", "psycopg")


def _new_database_url(postgres_container_url: str) -> Generator[str, None, None]:
    database_name = f"ariel_test_{uuid4().hex}"
    admin_engine = create_engine(
        postgres_container_url,
        future=True,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        with admin_engine.connect() as connection:
            database_identifier = connection.dialect.identifier_preparer.quote(database_name)
            connection.execute(text(f"CREATE DATABASE {database_identifier}"))

        try:
            yield (
                make_url(postgres_container_url)
                .set(database=database_name)
                .render_as_string(hide_password=False)
            )
        finally:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f"DROP DATABASE IF EXISTS {database_identifier}"))
    finally:
        admin_engine.dispose()


@pytest.fixture
def postgres_url(postgres_container_url: str) -> Generator[str, None, None]:
    yield from _new_database_url(postgres_container_url)


@pytest.fixture
def unmigrated_postgres_url(postgres_container_url: str) -> Generator[str, None, None]:
    yield from _new_database_url(postgres_container_url)


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    try:
        yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    finally:
        engine.dispose()
