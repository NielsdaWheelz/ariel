from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

import ariel.memory as memory_module
import ariel.worker as worker_module
from ariel.config import AppSettings
from ariel.db import reset_schema_for_tests
from ariel.persistence import (
    BackgroundTaskRecord,
    MemoryAssertionRecord,
    MemoryContextBlockRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityRecord,
    MemoryEvidenceRecord,
    MemoryExportArtifactRecord,
    MemoryProjectionJobRecord,
    SessionRecord,
)


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


@pytest.fixture
def session_factory(postgres_url: str) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    reset_schema_for_tests(engine, postgres_url)
    try:
        yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    finally:
        engine.dispose()


def _settings(**overrides: Any) -> AppSettings:
    return cast(Any, AppSettings)(_env_file=None, **overrides)


def _seed_session(
    db: Session,
    *,
    session_id: str,
    memory_mode: str,
    now: datetime,
) -> None:
    db.add(
        SessionRecord(
            id=session_id,
            is_active=True,
            lifecycle_state="active",
            memory_mode=memory_mode,
            rotated_from_session_id=None,
            rotation_reason=None,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_memory_extract_task(
    session_factory: sessionmaker[Session],
    *,
    session_id: str,
    task_id: str,
    memory_mode: str,
    max_attempts: int,
    now: datetime,
) -> None:
    with session_factory() as db:
        with db.begin():
            _seed_session(db, session_id=session_id, memory_mode=memory_mode, now=now)
            db.flush()
            db.add(
                MemoryEvidenceRecord(
                    id=f"mev_{task_id}",
                    source_turn_id=None,
                    source_session_id=session_id,
                    actor_id="user.local",
                    content_class="user_message",
                    trust_boundary="trusted_user",
                    lifecycle_state="available",
                    source_uri=None,
                    source_artifact_id=None,
                    source_text="Remember that I prefer black coffee.",
                    evidence_snippet="I prefer black coffee.",
                    redaction_posture="none",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                BackgroundTaskRecord(
                    id=task_id,
                    task_type="memory_extract_turn",
                    idempotency_key=None,
                    work_follow_up_loop_id=None,
                    work_follow_up_loop_version=None,
                    work_follow_up_scheduled_for=None,
                    provider_write_receipt_id=None,
                    payload={"evidence_id": f"mev_{task_id}", "session_id": session_id},
                    status="pending",
                    attempts=0,
                    max_attempts=max_attempts,
                    error=None,
                    claimed_by=None,
                    run_after=now,
                    last_heartbeat=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _seed_active_assertion(
    db: Session,
    *,
    assertion_id: str,
    now: datetime,
) -> None:
    entity_id = f"ent_{assertion_id}"
    db.add(
        MemoryEntityRecord(
            id=entity_id,
            entity_type="user",
            entity_key=assertion_id,
            display_name="Worker test user",
            summary=None,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryAssertionRecord(
            id=assertion_id,
            subject_entity_id=entity_id,
            subject_key=assertion_id,
            predicate="prefers",
            scope_key="global",
            object_value={"value": "black coffee"},
            assertion_type="preference",
            is_multi_valued=False,
            scope={},
            lifecycle_state="active",
            confidence=0.9,
            valid_from=None,
            valid_to=None,
            superseded_by_assertion_id=None,
            extraction_model=None,
            extraction_prompt_version=None,
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_projection_job(
    db: Session,
    *,
    job_id: str,
    projection_kind: str,
    target_table: str,
    target_id: str,
    max_retries: int,
    now: datetime,
) -> None:
    db.add(
        MemoryProjectionJobRecord(
            id=job_id,
            projection_kind=projection_kind,
            target_table=target_table,
            target_id=target_id,
            lifecycle_state="pending",
            attempts=0,
            max_retries=max_retries,
            error=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_background_task(
    db: Session,
    *,
    task_id: str,
    task_type: str,
    payload: Any,
    max_attempts: int,
    now: datetime,
) -> None:
    db.add(
        BackgroundTaskRecord(
            id=task_id,
            task_type=task_type,
            idempotency_key=None,
            work_follow_up_loop_id=None,
            work_follow_up_loop_version=None,
            work_follow_up_scheduled_for=None,
            provider_write_receipt_id=None,
            payload=payload,
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            error=None,
            claimed_by=None,
            run_after=now,
            last_heartbeat=None,
            created_at=now,
            updated_at=now,
        )
    )


def test_process_one_task_completes_memory_extract_turn_when_session_blocks_memory(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    _seed_memory_extract_task(
        session_factory,
        session_id="ses_mem_extract_noop",
        task_id="tsk_mem_extract_noop",
        memory_mode="no_memory",
        max_attempts=2,
        now=now,
    )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(openai_api_key=None),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_mem_extract_noop")
            assert task is not None
            assert task.status == "completed"
            assert task.attempts == 1
            assert task.error is None
            assert task.claimed_by is None
            assert task.last_heartbeat is None


def test_process_one_task_retries_memory_extract_turn_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 5, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    _seed_memory_extract_task(
        session_factory,
        session_id="ses_mem_extract_fail",
        task_id="tsk_mem_extract_fail",
        memory_mode="normal",
        max_attempts=2,
        now=now,
    )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(openai_api_key=None),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_mem_extract_fail")
            assert task is not None
            assert task.status == "pending"
            assert task.attempts == 1
            assert task.error == "unexpected RuntimeError"
            assert task.claimed_by is None
            assert task.last_heartbeat is None
            assert task.run_after == now + timedelta(seconds=1)


def test_process_one_task_completes_embedding_projection_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 10, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    monkeypatch.setattr(
        memory_module,
        "embed_memory_text",
        lambda text, *, settings: [0.0] * settings.memory_embedding_dimensions,
    )
    with session_factory() as db:
        with db.begin():
            _seed_active_assertion(db, assertion_id="mas_embed_ok", now=now)
            _seed_projection_job(
                db,
                job_id="mpj_embed_ok",
                projection_kind="embedding",
                target_table="memory_assertions",
                target_id="mas_embed_ok",
                max_retries=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_embed_ok")
            projection_count = db.scalar(
                select(func.count())
                .select_from(MemoryEmbeddingProjectionRecord)
                .where(MemoryEmbeddingProjectionRecord.assertion_id == "mas_embed_ok")
            )
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 1
            assert job.error is None
            assert projection_count == 1


def test_process_one_task_retries_embedding_projection_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)

    def fail_embedding(text: str, *, settings: AppSettings) -> list[float]:
        del text, settings
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(memory_module, "embed_memory_text", fail_embedding)
    with session_factory() as db:
        with db.begin():
            _seed_active_assertion(db, assertion_id="mas_embed_retry", now=now)
            _seed_projection_job(
                db,
                job_id="mpj_embed_retry",
                projection_kind="embedding",
                target_table="memory_assertions",
                target_id="mas_embed_retry",
                max_retries=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_embed_retry")
            assert job is not None
            assert job.lifecycle_state == "pending"
            assert job.attempts == 1
            assert job.error == "embedding unavailable"
            assert job.run_after == now + timedelta(seconds=30)


def test_process_one_task_recovers_and_retries_stale_running_embedding_projection_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 17, tzinfo=UTC)
    stale_updated_at = now - timedelta(minutes=10)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    monkeypatch.setattr(
        memory_module,
        "embed_memory_text",
        lambda text, *, settings: [0.0] * settings.memory_embedding_dimensions,
    )
    with session_factory() as db:
        with db.begin():
            _seed_active_assertion(db, assertion_id="mas_embed_stale", now=now)
            _seed_projection_job(
                db,
                job_id="mpj_embed_stale",
                projection_kind="embedding",
                target_table="memory_assertions",
                target_id="mas_embed_stale",
                max_retries=3,
                now=stale_updated_at,
            )
            job = db.get(MemoryProjectionJobRecord, "mpj_embed_stale")
            assert job is not None
            job.lifecycle_state = "running"
            job.attempts = 1
            job.updated_at = stale_updated_at

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(worker_heartbeat_timeout_seconds=60),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_embed_stale")
            projection_count = db.scalar(
                select(func.count())
                .select_from(MemoryEmbeddingProjectionRecord)
                .where(MemoryEmbeddingProjectionRecord.assertion_id == "mas_embed_stale")
            )
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 2
            assert job.error is None
            assert projection_count == 1


def test_process_one_task_dead_letters_unsupported_memory_projection_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 18, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_keyword_unsupported",
                projection_kind="keyword",
                target_table="memory_assertions",
                target_id="mas_keyword_unsupported",
                max_retries=3,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_keyword_unsupported")
            assert job is not None
            assert job.lifecycle_state == "dead_letter"
            assert job.attempts == 1
            assert job.error == "unsupported memory projection job: keyword"
            assert job.run_after == now


def test_process_one_task_dead_letters_malformed_embedding_projection_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 19, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_embedding_bad_target",
                projection_kind="embedding",
                target_table="memory_scopes",
                target_id="global",
                max_retries=3,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_embedding_bad_target")
            assert job is not None
            assert job.lifecycle_state == "dead_letter"
            assert job.attempts == 1
            assert job.error == "malformed embedding projection target table: memory_scopes"


def test_process_one_task_completes_memory_consolidation_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 20, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_hot_index",
                projection_kind="hot_index",
                target_table="memory_scopes",
                target_id="global",
                max_retries=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_hot_index")
            block = db.scalar(
                select(MemoryContextBlockRecord)
                .where(
                    MemoryContextBlockRecord.block_type == "hot_index",
                    MemoryContextBlockRecord.scope_key == "global",
                )
                .limit(1)
            )
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 1
            assert job.error is None
            assert block is not None
            assert block.lifecycle_state == "active"


def test_process_one_task_dead_letters_malformed_memory_maintenance_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 21, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_hot_index_bad_target",
                projection_kind="hot_index",
                target_table="memory_assertions",
                target_id="mas_bad",
                max_retries=1,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_hot_index_bad_target")
            assert job is not None
            assert job.lifecycle_state == "dead_letter"
            assert job.attempts == 1
            assert (
                job.error
                == "memory maintenance job target table must be memory_scopes: memory_assertions"
            )


def test_process_one_task_accepts_long_memory_maintenance_scope_key(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 21, 30, tzinfo=UTC)
    scope_key = "project:" + "phoenix-" * 8
    observed: dict[str, Any] = {}
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)

    def consolidate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["scope_key"] = kwargs["scope_key"]
        return {"scope_key": kwargs["scope_key"]}

    monkeypatch.setattr(worker_module, "consolidate_memory", consolidate)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_hot_index_long_scope",
                projection_kind="hot_index",
                target_table="memory_scopes",
                target_id=scope_key,
                max_retries=1,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_hot_index_long_scope")
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert observed["scope_key"] == scope_key


def test_process_one_task_completes_memory_export_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 22, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_active_assertion(db, assertion_id="mas_export_ok", now=now)
            _seed_projection_job(
                db,
                job_id="mpj_export_ok",
                projection_kind="export",
                target_table="memory_scopes",
                target_id="global",
                max_retries=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_export_ok")
            artifact = db.scalar(
                select(MemoryExportArtifactRecord)
                .where(MemoryExportArtifactRecord.scope_key == "global")
                .limit(1)
            )
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 1
            assert job.error is None
            assert artifact is not None
            assert artifact.status == "created"
            assert artifact.source_counts["active_assertions"] == 1


def test_process_one_task_retries_memory_export_job_failure(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 25, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)

    def fail_export(
        db: Session,
        *,
        scope_key: str,
        actor_id: str,
        now_fn: Any,
        new_id_fn: Any,
    ) -> dict[str, Any]:
        del db, scope_key, actor_id, now_fn, new_id_fn
        raise RuntimeError("export store unavailable")

    monkeypatch.setattr(worker_module, "export_memory", fail_export)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_export_retry",
                projection_kind="export",
                target_table="memory_scopes",
                target_id="global",
                max_retries=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_export_retry")
            artifact_count = db.scalar(select(func.count()).select_from(MemoryExportArtifactRecord))
            assert job is not None
            assert job.lifecycle_state == "pending"
            assert job.attempts == 1
            assert job.error == "export store unavailable"
            assert job.run_after == now + timedelta(seconds=1)
            assert artifact_count == 0


def test_process_one_task_dead_letters_memory_export_job_after_last_attempt(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)

    def fail_export(
        db: Session,
        *,
        scope_key: str,
        actor_id: str,
        now_fn: Any,
        new_id_fn: Any,
    ) -> dict[str, Any]:
        del db, scope_key, actor_id, now_fn, new_id_fn
        raise RuntimeError("export store unavailable")

    monkeypatch.setattr(worker_module, "export_memory", fail_export)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_export_dead",
                projection_kind="export",
                target_table="memory_scopes",
                target_id="global",
                max_retries=1,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            job = db.get(MemoryProjectionJobRecord, "mpj_export_dead")
            artifact_count = db.scalar(select(func.count()).select_from(MemoryExportArtifactRecord))
            assert job is not None
            assert job.lifecycle_state == "dead_letter"
            assert job.attempts == 1
            assert job.error == "export store unavailable"
            assert job.run_after == now
            assert artifact_count == 0


def test_process_one_task_retries_then_dead_letters_malformed_task_payload(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 12, 35, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_background_task(
                db,
                task_id="tsk_malformed_payload",
                task_type="memory_extract_turn",
                payload=[],
                max_attempts=2,
                now=now,
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_malformed_payload")
            assert task is not None
            assert task.status == "pending"
            assert task.attempts == 1
            assert task.error == "memory_extract_turn task payload invalid"
            assert task.run_after == now + timedelta(seconds=1)

    retry_now = now + timedelta(seconds=1)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: retry_now)

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            task = db.get(BackgroundTaskRecord, "tsk_malformed_payload")
            assert task is not None
            assert task.status == "dead_letter"
            assert task.attempts == 2
            assert task.error == "memory_extract_turn task payload invalid"
            assert task.run_after == retry_now
