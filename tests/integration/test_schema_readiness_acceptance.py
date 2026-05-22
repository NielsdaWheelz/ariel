from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text

from ariel.app import create_app
from ariel.db import SchemaReadinessProbe, run_migrations, schema_readiness_issues
from ariel.persistence import SubscriberHeartbeatRecord
from tests.fake_sandbox import FakeSandboxRuntime


@dataclass
class NoCallModelAdapter:
    provider: str = "provider.schema-readiness"
    model: str = "model.schema-readiness-v1"

    def create_response(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("schema readiness tests must not call the model adapter")


def test_schema_not_ready_returns_503_until_migrated(unmigrated_postgres_url: str) -> None:
    adapter = NoCallModelAdapter()

    app_without_migration = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app_without_migration) as client:
        health = client.get("/v1/health")
        assert health.status_code == 503
        health_body = health.json()
        assert health_body["ok"] is False
        assert health_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        assert "schema_issues" in health_body["error"]["details"]
        assert "missing_tables" not in health_body["error"]["details"]

        active = client.get("/v1/sessions/active")
        assert active.status_code == 503
        active_body = active.json()
        assert active_body["ok"] is False
        assert active_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        assert "schema_issues" in active_body["error"]["details"]
        assert "missing_tables" not in active_body["error"]["details"]

    run_migrations(unmigrated_postgres_url)
    app_with_migration = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app_with_migration) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/sessions/active").status_code == 200


def test_schema_readiness_recovers_when_migrations_land_after_startup(
    unmigrated_postgres_url: str,
) -> None:
    # The TTL-cached probe must reflect migrations that land after startup.
    adapter = NoCallModelAdapter()
    app = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    fake_now = [0.0]

    def fake_clock() -> float:
        return fake_now[0]

    ttl_seconds = 10.0
    app.state.schema_probe = SchemaReadinessProbe(
        app.state.engine,
        ttl_seconds=ttl_seconds,
        clock=fake_clock,
    )
    with TestClient(app) as client:
        first = client.get("/v1/health")
        assert first.status_code == 503
        first_body = first.json()
        assert first_body["error"]["code"] == "E_SCHEMA_NOT_READY"
        first_schema_issues = first_body["error"]["details"]["schema_issues"]
        assert first_schema_issues

        cached = client.get("/v1/health")
        assert cached.status_code == 503
        assert cached.json()["error"]["details"]["schema_issues"] == first_schema_issues

        run_migrations(unmigrated_postgres_url)

        stale = client.get("/v1/health")
        assert stale.status_code == 503

        fake_now[0] += ttl_seconds + 0.1

        recovered = client.get("/v1/health")
        assert recovered.status_code == 200

        assert client.get("/v1/sessions/active").status_code == 200


def test_schema_readiness_reports_missing_subscriber_heartbeat(postgres_url: str) -> None:
    run_migrations(postgres_url)
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE subscriber_heartbeat"))

        assert "missing_table:subscriber_heartbeat" in schema_readiness_issues(engine)
    finally:
        engine.dispose()


def test_schema_readiness_reports_event_created_at_columns(postgres_url: str) -> None:
    run_migrations(postgres_url)
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE provider_events DROP COLUMN created_at"))
            connection.execute(text("ALTER TABLE agency_events DROP COLUMN created_at"))

        schema_issues = schema_readiness_issues(engine)
        assert "missing_column:provider_events.created_at" in schema_issues
        assert "missing_column:agency_events.created_at" in schema_issues
    finally:
        engine.dispose()


def test_schema_readiness_reports_subscriber_heartbeat_identity_rules(
    postgres_url: str,
) -> None:
    run_migrations(postgres_url)
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE subscriber_heartbeat DROP CONSTRAINT pk_subscriber_heartbeat")
            )
            connection.execute(
                text(
                    "ALTER TABLE subscriber_heartbeat "
                    "ADD CONSTRAINT subscriber_heartbeat_pkey PRIMARY KEY (subscriber_name)"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE subscriber_heartbeat "
                    "DROP CONSTRAINT uq_subscriber_heartbeat_subscriber_name"
                )
            )

        schema_issues = schema_readiness_issues(engine)
        assert "wrong_primary_key:subscriber_heartbeat" in schema_issues
        assert (
            "missing_unique_constraint:"
            "subscriber_heartbeat.uq_subscriber_heartbeat_subscriber_name" in schema_issues
        )
    finally:
        engine.dispose()


def test_subscriber_heartbeat_identity_migration_backfills_existing_rows(
    postgres_url: str,
) -> None:
    run_migrations(postgres_url, revision="20260522_0059")
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO subscriber_heartbeat "
                    "(subscriber_name, last_seen_at, created_at, updated_at) "
                    "VALUES ('gmail_pubsub', now(), now(), now())"
                )
            )

        run_migrations(postgres_url)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, subscriber_name "
                    "FROM subscriber_heartbeat "
                    "WHERE subscriber_name = 'gmail_pubsub'"
                )
            ).one()

        assert row.id.startswith("shb_")
        assert len(row.id) <= 32
        assert schema_readiness_issues(engine) == []
    finally:
        engine.dispose()


def test_schema_readiness_reports_context_pressure_rotation_reason(
    postgres_url: str,
) -> None:
    run_migrations(postgres_url, revision="20260522_0060")
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        schema_issues = schema_readiness_issues(engine)
        assert "missing_alembic_head:20260522_0061" in schema_issues
        assert (
            "forbidden_constraint_fragment:sessions.ck_session_rotation_reason" in schema_issues
        )
        assert (
            "forbidden_constraint_fragment:"
            "session_rotations.ck_session_rotation_reason_type" in schema_issues
        )
    finally:
        engine.dispose()


def test_rotation_reason_schema_migration_rejects_context_pressure_rows(
    postgres_url: str,
) -> None:
    run_migrations(postgres_url, revision="20260522_0060")
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, is_active, lifecycle_state, created_at, updated_at) "
                    "VALUES ('ses_source', false, 'closed', now(), now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, is_active, lifecycle_state, rotated_from_session_id, "
                    "rotation_reason, created_at, updated_at) "
                    "VALUES ('ses_blocked', false, 'closed', 'ses_source', "
                    "'threshold_context_pressure', now(), now())"
                )
            )

        with pytest.raises(RuntimeError, match="rotation rows must be repaired first"):
            run_migrations(postgres_url)
    finally:
        engine.dispose()


def test_health_reads_subscriber_heartbeat_by_subscriber_name(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
        "projects/my-project/subscriptions/ariel-gmail-watch-sub",
    )
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "/tmp/ariel-sa.json")
    run_migrations(postgres_url)
    app = create_app(
        database_url=postgres_url,
        model_adapter=NoCallModelAdapter(),
        sandbox=FakeSandboxRuntime(),
    )
    now = datetime.now(tz=UTC)
    with app.state.session_factory() as db:
        with db.begin():
            db.add(
                SubscriberHeartbeatRecord(
                    id="shb_test",
                    subscriber_name="gmail_pubsub",
                    last_seen_at=now,
                    last_message_at=now,
                    in_flight_count=0,
                    errors_in_window=0,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    with TestClient(app) as client:
        health = client.get("/v1/health")

    assert health.status_code == 200
    assert health.json()["subscribers"]["gmail_pubsub"]["last_seen_at"] is not None
