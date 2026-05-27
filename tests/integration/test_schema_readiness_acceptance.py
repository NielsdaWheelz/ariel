from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ariel.app import create_app
from ariel.db import SchemaReadinessProbe, run_migrations, schema_readiness_issues
from ariel.model_adapter import ModelCall, ModelResponse
from ariel.persistence import SubscriberHeartbeatRecord
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import FakeModelAdapter


class NoCallModelAdapter(FakeModelAdapter):
    provider = "provider.schema-readiness"
    model = "model.schema-readiness-v1"

    def _respond(self, request: ModelCall) -> ModelResponse:
        del request
        raise AssertionError("schema readiness tests must not call the model adapter")


def _integrity_constraint_name(exc: IntegrityError) -> str:
    diag = getattr(exc.orig, "diag", None)
    return str(getattr(diag, "constraint_name", ""))


def _insert_google_connector(
    connection: Connection,
    *,
    connector_id: str,
    status: str,
    account_subject: str | None,
    account_email: str | None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO google_connectors "
            "(id, provider, status, account_subject, account_email, granted_scopes, "
            "encryption_key_version, created_at, updated_at) "
            "VALUES (:connector_id, 'google', :status, :account_subject, :account_email, "
            "'[]'::jsonb, 'v1', now(), now())"
        ),
        {
            "connector_id": connector_id,
            "status": status,
            "account_subject": account_subject,
            "account_email": account_email,
        },
    )


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

    run_migrations(unmigrated_postgres_url)
    app_with_migration = create_app(
        database_url=unmigrated_postgres_url,
        model_adapter=adapter,
        sandbox=FakeSandboxRuntime(),
    )
    with TestClient(app_with_migration) as client:
        assert client.get("/v1/health").status_code == 200


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


def test_health_reports_runtime_schema_and_provider_evidence_posture(
    postgres_url: str,
) -> None:
    app = create_app(
        database_url=postgres_url,
        model_adapter=NoCallModelAdapter(),
        sandbox=FakeSandboxRuntime(),
    )

    with TestClient(app) as client:
        health = client.get("/v1/health")

    assert health.status_code == 200
    payload = health.json()
    assert payload["ok"] is True
    assert payload["runtime"]["cwd"]
    assert "git_sha" in payload["runtime"]
    assert payload["schema"]["ready"] is True
    assert payload["schema"]["issues"] == []
    assert payload["schema"]["alembic_current_revisions"]
    assert payload["prompt"]["main_agent_prompt_version"]
    assert payload["capabilities"]["digest"]
    assert payload["capabilities"]["capability_count"] > 0
    assert payload["provider_evidence"]["surface"] == "ready"
    assert payload["provider_evidence"]["read_capability"] == "cap.provider_evidence.read"
    assert isinstance(payload["provider_evidence"]["available_rows"], int)
    assert isinstance(payload["provider_evidence"]["block_rows"], int)
    assert "google_sync" in payload


def test_schema_readiness_reports_missing_subscriber_heartbeat(postgres_url: str) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE subscriber_heartbeat"))

        assert "missing_table:subscriber_heartbeat" in schema_readiness_issues(engine)
    finally:
        engine.dispose()


def test_schema_readiness_reports_event_created_at_columns(postgres_url: str) -> None:
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


def test_schema_readiness_reports_wrong_foreign_key_ondelete(postgres_url: str) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE events DROP CONSTRAINT events_turn_id_fkey"))
            connection.execute(
                text(
                    "ALTER TABLE events "
                    "ADD CONSTRAINT events_turn_id_fkey "
                    "FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE"
                )
            )

        assert "wrong_foreign_key_ondelete:events.turn_id" in schema_readiness_issues(engine)
    finally:
        engine.dispose()


def test_schema_readiness_reports_missing_job_event_agency_event_unique_constraint(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE job_events DROP CONSTRAINT job_events_agency_event_id_key")
            )

        assert "missing_unique_constraint:job_events.agency_event_id" in schema_readiness_issues(
            engine
        )
    finally:
        engine.dispose()


def test_schema_readiness_reports_changed_partial_index_predicate(postgres_url: str) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_provider_write_receipts_idempotency_unique"))
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ix_provider_write_receipts_idempotency_unique "
                    "ON provider_write_receipts (provider, provider_account_id, idempotency_key)"
                )
            )

        assert (
            "missing_index_fragment:provider_write_receipts."
            "ix_provider_write_receipts_idempotency_unique" in schema_readiness_issues(engine)
        )
    finally:
        engine.dispose()


def test_schema_readiness_reports_changed_check_constraint_column(postgres_url: str) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE captures DROP CONSTRAINT ck_capture_kind"))
            connection.execute(
                text(
                    "ALTER TABLE captures "
                    "ADD CONSTRAINT ck_capture_kind "
                    "CHECK (request_hash IN ('text', 'url', 'shared_content'))"
                )
            )

        assert "missing_constraint_fragment:captures.ck_capture_kind" in schema_readiness_issues(
            engine
        )
    finally:
        engine.dispose()


def test_schema_readiness_reports_google_connector_identity_constraint_drift(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE google_connectors "
                    "DROP CONSTRAINT ck_google_connector_connected_account_email"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE google_connectors "
                    "ADD CONSTRAINT ck_google_connector_connected_account_email "
                    "CHECK (status <> 'connected' OR account_subject IS NOT NULL)"
                )
            )

        assert (
            "missing_constraint_fragment:"
            "google_connectors.ck_google_connector_connected_account_email"
            in schema_readiness_issues(engine)
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("connector_id", "account_subject", "account_email", "constraint_name"),
    [
        (
            "con_no_subject",
            None,
            "owner@example.com",
            "ck_google_connector_connected_account_subject",
        ),
        (
            "con_blank_subject",
            "",
            "owner@example.com",
            "ck_google_connector_connected_account_subject",
        ),
        (
            "con_space_subject",
            "sub owner",
            "owner@example.com",
            "ck_google_connector_connected_account_subject",
        ),
        ("con_no_email", "sub_owner", None, "ck_google_connector_connected_account_email"),
        ("con_blank_email", "sub_owner", "", "ck_google_connector_connected_account_email"),
        (
            "con_space_email",
            "sub_owner",
            "owner @example.com",
            "ck_google_connector_connected_account_email",
        ),
        (
            "con_bad_email",
            "sub_owner",
            "owner.example.com",
            "ck_google_connector_connected_account_email",
        ),
    ],
)
def test_google_connector_connected_identity_is_check_constrained(
    postgres_url: str,
    connector_id: str,
    account_subject: str | None,
    account_email: str | None,
    constraint_name: str,
) -> None:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    try:
        with pytest.raises(IntegrityError) as exc_info:
            with engine.begin() as connection:
                _insert_google_connector(
                    connection,
                    connector_id=connector_id,
                    status="connected",
                    account_subject=account_subject,
                    account_email=account_email,
                )

        assert _integrity_constraint_name(exc_info.value) == constraint_name

        with engine.begin() as connection:
            _insert_google_connector(
                connection,
                connector_id=f"{connector_id}_off",
                status="not_connected",
                account_subject=None,
                account_email=None,
            )
            _insert_google_connector(
                connection,
                connector_id=f"{connector_id}_on",
                status="connected",
                account_subject="sub_owner",
                account_email="owner@example.com",
            )
    finally:
        engine.dispose()


def test_google_connector_connected_identity_migration_reclassifies_invalid_rows(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260522_0063")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            _insert_google_connector(
                connection,
                connector_id="con_google",
                status="connected",
                account_subject=None,
                account_email=" ",
            )

        run_migrations(unmigrated_postgres_url)

        with engine.connect() as connection:
            connector = (
                connection.execute(
                    text(
                        "SELECT status, account_subject, account_email, last_error_code, "
                        "last_error_at "
                        "FROM google_connectors WHERE id = 'con_google'"
                    )
                )
                .mappings()
                .one()
            )

        assert connector["status"] == "error"
        assert connector["account_subject"] is None
        assert connector["account_email"] is None
        assert connector["last_error_code"] == "account_identity_missing"
        assert connector["last_error_at"] is not None
        assert schema_readiness_issues(engine) == []
    finally:
        engine.dispose()


def test_subscriber_heartbeat_identity_migration_backfills_existing_rows(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260522_0059")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO subscriber_heartbeat "
                    "(subscriber_name, last_seen_at, created_at, updated_at) "
                    "VALUES ('gmail_pubsub', now(), now(), now())"
                )
            )

        run_migrations(unmigrated_postgres_url)

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


def test_schema_readiness_reports_stale_capture_ingress_schema(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260522_0062")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        schema_issues = schema_readiness_issues(engine)
        assert "missing_alembic_head:20260527_0071" in schema_issues
        assert "unexpected_column:captures.original_payload" in schema_issues
        assert "unexpected_column:captures.terminal_state" in schema_issues
        assert "unexpected_constraint:captures.ck_capture_terminal_state" in schema_issues
        assert "unexpected_constraint:captures.ck_capture_terminal_linkage" in schema_issues
        assert "forbidden_constraint_fragment:captures.ck_capture_kind" in schema_issues
    finally:
        engine.dispose()


def test_capture_schema_migration_rejects_rows_outside_durable_record_shape(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260522_0062")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO captures "
                    "(id, capture_kind, idempotency_key, request_hash, original_payload, "
                    "normalized_turn_input, turn_id, terminal_state, "
                    "ingest_error_code, ingest_error_message, ingest_error_details, "
                    "ingest_error_retryable, status_code, response_payload, created_at, updated_at) "
                    "VALUES ('cpt_bad', 'unknown', NULL, :request_hash, '{}'::jsonb, "
                    "NULL, NULL, 'ingest_failed', 'bad_capture', "
                    "'bad capture', '{}'::jsonb, false, 422, '{}'::jsonb, now(), now())"
                ),
                {"request_hash": "0" * 64},
            )

        with pytest.raises(
            RuntimeError,
            match="capture rows must be repaired before capture schema narrowing",
        ):
            run_migrations(unmigrated_postgres_url)
    finally:
        engine.dispose()


def test_capture_schema_migration_removes_raw_payload_and_dead_state(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260522_0062")
    run_migrations(unmigrated_postgres_url)
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            columns = {
                str(row.column_name): bool(row.is_nullable == "YES")
                for row in connection.execute(
                    text(
                        "SELECT column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'captures'"
                    )
                )
            }
            constraints = {
                str(row.constraint_name)
                for row in connection.execute(
                    text(
                        "SELECT constraint_name "
                        "FROM information_schema.table_constraints "
                        "WHERE table_name = 'captures' AND constraint_type = 'CHECK'"
                    )
                )
                if not str(row.constraint_name).endswith("_not_null")
            }

        assert "original_payload" not in columns
        assert "terminal_state" not in columns
        assert "ingest_error_code" not in columns
        assert "status_code" not in columns
        assert "response_payload" not in columns
        assert columns["normalized_turn_input"] is False
        assert columns["turn_id"] is False
        assert constraints == {"ck_capture_kind"}
        assert schema_readiness_issues(engine) == []
    finally:
        engine.dispose()


def test_schema_readiness_reports_undispatched_background_task_types(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260523_0064")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        schema_issues = schema_readiness_issues(engine)
        assert "missing_alembic_head:20260527_0071" in schema_issues
        assert (
            "forbidden_constraint_fragment:background_tasks.ck_background_task_type"
            in schema_issues
        )
    finally:
        engine.dispose()


def test_schema_readiness_reports_missing_turn_idempotency_owner_constraint(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260524_0067")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        schema_issues = schema_readiness_issues(engine)
        assert "missing_constraint:turn_idempotency_keys.ck_turn_idempotency_has_owner" in (
            schema_issues
        )
    finally:
        engine.dispose()


def test_background_task_schema_migration_rejects_undispatched_task_rows(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260523_0064")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO background_tasks "
                    "(id, task_type, payload, attempts, run_after, created_at, updated_at) "
                    "VALUES ('tsk_dead_google_object', 'google_object_hydration_due', "
                    "'{}'::jsonb, 0, now(), now(), now())"
                )
            )

        with pytest.raises(RuntimeError, match="undispatched background task rows"):
            run_migrations(unmigrated_postgres_url)
    finally:
        engine.dispose()


def test_background_task_schema_migration_removes_undispatched_task_types(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260523_0064")
    run_migrations(unmigrated_postgres_url)
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with pytest.raises(IntegrityError) as exc_info:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO background_tasks "
                        "(id, task_type, payload, attempts, run_after, created_at, updated_at) "
                        "VALUES ('tsk_dead_provider_evidence', "
                        "'provider_evidence_extraction_due', "
                        "'{}'::jsonb, 0, now(), now(), now())"
                    )
                )

        assert _integrity_constraint_name(exc_info.value) == "ck_background_task_type"
        assert schema_readiness_issues(engine) == []
    finally:
        engine.dispose()


def test_action_success_event_payload_migration_cuts_over_legacy_output(
    unmigrated_postgres_url: str,
) -> None:
    run_migrations(unmigrated_postgres_url, revision="20260526_0070")
    engine = create_engine(unmigrated_postgres_url, future=True, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO turns "
                    "(id, user_message, assistant_message, status, created_at, updated_at) "
                    "VALUES ('trn_legacy_success', 'legacy', NULL, 'completed', now(), now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO action_attempts "
                    "(id, turn_id, proposal_index, capability_id, capability_version, "
                    "capability_contract_hash, impact_level, proposed_input, payload_hash, "
                    "policy_decision, policy_reason, status, approval_required, "
                    "execution_output, execution_error, created_at, updated_at) "
                    "VALUES ('aat_legacy_success', 'trn_legacy_success', 1, "
                    "'cap.email.read', '1.0', :contract_hash, 'read', '{}'::jsonb, "
                    ":payload_hash, 'allow_inline', NULL, 'succeeded', false, "
                    "'{}'::jsonb, NULL, now(), now())"
                ),
                {"contract_hash": "c" * 64, "payload_hash": "p" * 64},
            )
            connection.execute(
                text(
                    "INSERT INTO events "
                    "(id, turn_id, sequence, event_type, payload, created_at) "
                    "VALUES ('evt_legacy_success', 'trn_legacy_success', 1, "
                    "'evt.action.execution.succeeded', CAST(:payload AS jsonb), now())"
                ),
                {
                    "payload": (
                        '{"action_attempt_id":"aat_legacy_success",'
                        '"output":{"read_outcome":{"status":"ok"}},'
                        '"provider_write_receipt_id":"pwr_legacy"}'
                    )
                },
            )

        run_migrations(unmigrated_postgres_url)

        with engine.connect() as connection:
            payload = connection.execute(
                text("SELECT payload FROM events WHERE id = 'evt_legacy_success'")
            ).scalar_one()

        assert payload == {
            "action_attempt_id": "aat_legacy_success",
            "capability_id": "cap.email.read",
            "status": "succeeded",
            "execution_output": {"read_outcome": {"status": "ok"}},
            "provider_write_receipt_id": "pwr_legacy",
        }
        assert schema_readiness_issues(engine) == []
    finally:
        engine.dispose()


def test_health_reads_subscriber_heartbeat_by_subscriber_name(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_TOPIC",
        "projects/my-project/topics/ariel-gmail-watch",
    )
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
        "projects/my-project/subscriptions/ariel-gmail-watch-sub",
    )
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "/tmp/ariel-sa.json")
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
