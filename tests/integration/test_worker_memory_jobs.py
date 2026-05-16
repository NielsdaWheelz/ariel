from __future__ import annotations

from datetime import UTC, datetime, timedelta
import itertools
import json
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import ariel.memory as memory_module
import ariel.worker as worker_module
from ariel.config import AppSettings
from ariel.persistence import (
    BackgroundTaskRecord,
    MemoryAssertionRecord,
    MemoryContextBlockRecord,
    MemoryDeletionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityRecord,
    MemoryEvidenceRecord,
    MemoryExportArtifactRecord,
    MemoryGraphProjectionRecord,
    MemoryProcedureRecord,
    MemoryProjectionJobRecord,
    MemoryRelationshipRecord,
    MemoryRetentionPolicyRecord,
    MemoryReviewRecord,
    MemorySalienceRecord,
    MemoryTopicRecord,
    MemoryVersionRecord,
    SessionRecord,
)


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


def test_out_of_band_projection_job_kind_is_rejected_by_schema_check(
    session_factory: sessionmaker[Session],
) -> None:
    # The reconciled ck_memory_projection_job_kind CHECK rejects any kind that is
    # not enqueued and consumed. This is the stronger guarantee than a
    # dead-letter worker: a bad kind cannot be written in the first place.
    now = datetime(2026, 5, 13, 12, 18, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="ck_memory_projection_job_kind"):
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


def _seed_graph_scenario(
    db: Session,
    *,
    now: datetime,
) -> None:
    for suffix in ("a", "b", "c"):
        db.add(
            MemoryEntityRecord(
                id=f"men_graph_{suffix}",
                entity_type="project",
                entity_key=f"project:graph-{suffix}",
                display_name=f"Graph {suffix}",
                summary=None,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )
    _seed_session(db, session_id="ses_graph", memory_mode="normal", now=now)
    db.flush()
    db.add(
        MemoryEvidenceRecord(
            id="mev_graph",
            source_turn_id=None,
            source_session_id="ses_graph",
            actor_id="user.local",
            content_class="system",
            trust_boundary="system",
            lifecycle_state="available",
            source_uri=None,
            source_artifact_id=None,
            source_text="graph evidence",
            evidence_snippet="graph evidence",
            redaction_posture="none",
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    for suffix, source, target in (("ab", "a", "b"), ("bc", "b", "c")):
        db.add(
            MemoryRelationshipRecord(
                id=f"mrl_graph_{suffix}",
                source_entity_id=f"men_graph_{source}",
                target_entity_id=f"men_graph_{target}",
                relationship_type="depends_on",
                scope_key="global",
                lifecycle_state="active",
                confidence=0.9,
                valid_from=now,
                valid_to=None,
                evidence_id="mev_graph",
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )


def test_process_one_task_completes_graph_projection_job(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 13, 0, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_graph_scenario(db, now=now)
            _seed_projection_job(
                db,
                job_id="mpj_graph_ok",
                projection_kind="graph",
                target_table="memory_entities",
                target_id="men_graph_a",
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
            job = db.get(MemoryProjectionJobRecord, "mpj_graph_ok")
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 1
            assert job.error is None
            two_hop = db.scalar(
                select(MemoryGraphProjectionRecord).where(
                    MemoryGraphProjectionRecord.source_entity_id == "men_graph_a",
                    MemoryGraphProjectionRecord.target_entity_id == "men_graph_c",
                )
            )
            assert two_hop is not None
            assert two_hop.distance == 2
            assert len(two_hop.relationship_path) == 2


def test_process_one_task_dead_letters_graph_projection_job_with_missing_entity(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 13, 5, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    with session_factory() as db:
        with db.begin():
            _seed_projection_job(
                db,
                job_id="mpj_graph_missing",
                projection_kind="graph",
                target_table="memory_entities",
                target_id="men_graph_absent",
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
            job = db.get(MemoryProjectionJobRecord, "mpj_graph_missing")
            assert job is not None
            assert job.lifecycle_state == "dead_letter"
            assert job.attempts == 1
            assert job.error == "malformed graph projection missing source entity"


def test_process_one_task_enqueues_scheduled_consolidation_for_stale_scope(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 13, 13, 10, tzinfo=UTC)
    monkeypatch.setattr(worker_module, "_utcnow", lambda: now)
    stale_at = now - timedelta(seconds=200_000)
    with session_factory() as db:
        with db.begin():
            db.add(
                MemoryContextBlockRecord(
                    id="mcb_stale_hot_index",
                    block_type="hot_index",
                    scope_key="global",
                    content="{}",
                    topic_id=None,
                    lifecycle_state="active",
                    source_assertion_ids=[],
                    source_episode_ids=[],
                    source_trace_ids=[],
                    source_action_trace_ids=[],
                    source_procedure_ids=[],
                    source_project_state_snapshot_ids=[],
                    source_memory_versions={},
                    source_projection_versions={},
                    projection_version=memory_module.MEMORY_PROJECTION_VERSION,
                    created_at=stale_at,
                    updated_at=stale_at,
                )
            )

    assert worker_module.process_one_task(
        session_factory=session_factory,
        settings=_settings(memory_consolidation_interval_seconds=86_400),
        worker_id="worker-memory",
    )

    with session_factory() as db:
        with db.begin():
            consolidation_jobs = db.scalars(
                select(MemoryProjectionJobRecord).where(
                    MemoryProjectionJobRecord.projection_kind == "hot_index",
                    MemoryProjectionJobRecord.target_table == "memory_scopes",
                    MemoryProjectionJobRecord.target_id == "global",
                )
            ).all()
            # The stale-scope cadence enqueued exactly one consolidation job, then
            # the maintenance worker claimed and completed it on the same tick.
            assert len(consolidation_jobs) == 1
            assert consolidation_jobs[0].lifecycle_state == "completed"


def test_projection_health_reports_seeded_dead_lettered_job(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 5, 13, 13, 15, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_session(db, session_id="ses_health", memory_mode="normal", now=now)
            _seed_projection_job(
                db,
                job_id="mpj_dead_letter_health",
                projection_kind="embedding",
                target_table="memory_assertions",
                target_id="mas_health_absent",
                max_retries=0,
                now=now,
            )
            job = db.get(MemoryProjectionJobRecord, "mpj_dead_letter_health")
            assert job is not None
            job.lifecycle_state = "dead_letter"
            job.error = "seeded dead letter"

    with session_factory() as db:
        with db.begin():
            context, _event = memory_module.build_memory_context(
                db,
                user_message="anything",
                max_recalled_assertions=8,
                settings=_settings(),
                current_session_id="ses_health",
            )
    health = context["projection_health"]
    assert health["dead_letter_projection_jobs"] == 1
    assert health["failed_projection_jobs"] == 0
    assert health["stale_projection_count"] == 0


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
            _seed_active_assertion(db, assertion_id="mas_consolidate_topic", now=now)
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
            hot_index_block = db.scalar(
                select(MemoryContextBlockRecord)
                .where(
                    MemoryContextBlockRecord.block_type == "hot_index",
                    MemoryContextBlockRecord.scope_key == "global",
                )
                .limit(1)
            )
            topic_block = db.scalar(
                select(MemoryContextBlockRecord)
                .where(
                    MemoryContextBlockRecord.block_type == "topic",
                    MemoryContextBlockRecord.scope_key == "global",
                )
                .limit(1)
            )
            assert job is not None
            assert job.lifecycle_state == "completed"
            assert job.attempts == 1
            assert job.error is None
            # The worker-run consolidation rebuilt both the hot index and the
            # topic block for the seeded active assertion.
            assert hot_index_block is not None
            assert hot_index_block.lifecycle_state == "active"
            assert topic_block is not None
            assert topic_block.lifecycle_state == "active"
            assert topic_block.topic_id is not None


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


_hot_index_id_counter = itertools.count()


def _hot_index_new_id(prefix: str) -> str:
    return f"{prefix}_hib_{next(_hot_index_id_counter)}"


def _seed_scoped_assertion(
    db: Session,
    *,
    assertion_id: str,
    assertion_type: str,
    predicate: str,
    text: str,
    now: datetime,
) -> None:
    entity_id = f"ent_{assertion_id}"
    db.add(
        MemoryEntityRecord(
            id=entity_id,
            entity_type="user",
            entity_key=assertion_id,
            display_name="Hot index test user",
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
            subject_key="global",
            predicate=predicate,
            scope_key="global",
            object_value={"text": text},
            assertion_type=assertion_type,
            is_multi_valued=True,
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


def test_consolidate_memory_keeps_rebuilt_hot_index_within_budget(
    session_factory: sessionmaker[Session],
) -> None:
    # A scope with many active memories produces a hot index inside the default
    # 1500-token budget, and a tight budget forces the rebuild to evict the
    # lowest-salience entries until the index fits.
    now = datetime(2026, 5, 15, 9, 0, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            for index in range(60):
                _seed_scoped_assertion(
                    db,
                    assertion_id=f"mas_hot_budget_{index:02d}",
                    assertion_type="preference",
                    predicate=f"preference.code_style.{index:02d}",
                    text=("verbatim preference detail " * 40),
                    now=now,
                )

    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key="global",
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    with session_factory() as db:
        default_block = db.scalar(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.block_type == "hot_index",
                MemoryContextBlockRecord.scope_key == "global",
            )
        )
        assert default_block is not None
        assert memory_module.count_context_tokens(default_block.content) <= 1500
        full_entry_count = json.loads(default_block.content)["entry_count"]

    # Re-run with a budget below the natural index size: eviction must shrink the
    # rebuilt index to fit, dropping the lowest-salience entries.
    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key="global",
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
                settings=_settings(
                    memory_hot_index_budget_tokens=40,
                    memory_hot_index_hard_max_tokens=2500,
                ),
            )

    with session_factory() as db:
        tight_block = db.scalar(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.block_type == "hot_index",
                MemoryContextBlockRecord.scope_key == "global",
            )
        )
        assert tight_block is not None
        assert memory_module.count_context_tokens(tight_block.content) <= 40
        assert json.loads(tight_block.content)["entry_count"] < full_entry_count


def test_consolidate_memory_raises_when_rebuilt_hot_index_exceeds_hard_max(
    session_factory: sessionmaker[Session],
) -> None:
    # The "do not repeat" section is policy-mandated and is not evicted, so a
    # scope with enough negative memory and a low hard max cannot be made to fit:
    # an over-budget rebuild is a defect and raises MemoryProjectionError.
    now = datetime(2026, 5, 15, 9, 5, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            for index in range(40):
                _seed_scoped_assertion(
                    db,
                    assertion_id=f"mas_hot_neg_{index:02d}",
                    assertion_type="negative",
                    predicate="negative.rejected_approach",
                    text="rejected approach detail",
                    now=now,
                )

    with pytest.raises(memory_module.MemoryProjectionError, match="hard max"):
        with session_factory() as db:
            with db.begin():
                memory_module.consolidate_memory(
                    db,
                    scope_key="global",
                    actor_id="system",
                    now_fn=lambda: now,
                    new_id_fn=_hot_index_new_id,
                    settings=_settings(
                        memory_hot_index_budget_tokens=20,
                        memory_hot_index_hard_max_tokens=60,
                    ),
                )


def test_consolidate_memory_hot_index_entries_carry_source_ids(
    session_factory: sessionmaker[Session],
) -> None:
    # Every hot-index entry, in both the salience-ranked section and the "do not
    # repeat" section, references its source assertions by id and never carries
    # verbatim memory values.
    now = datetime(2026, 5, 15, 9, 10, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_scoped_assertion(
                db,
                assertion_id="mas_hot_entry_pref",
                assertion_type="preference",
                predicate="preference.code_style",
                text="prefers explicit typing",
                now=now,
            )
            _seed_scoped_assertion(
                db,
                assertion_id="mas_hot_entry_neg",
                assertion_type="negative",
                predicate="negative.rejected_approach",
                text="do not use the legacy adapter",
                now=now,
            )

    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key="global",
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    with session_factory() as db:
        block = db.scalar(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.block_type == "hot_index",
                MemoryContextBlockRecord.scope_key == "global",
            )
        )
        assert block is not None
        content = json.loads(block.content)
        entries = content["entries"]
        do_not_repeat = content["do_not_repeat"]
        assert entries and do_not_repeat
        for entry in [*entries, *do_not_repeat]:
            assert entry["source_assertion_ids"]
            assert all(isinstance(ref, str) and ref for ref in entry["source_assertion_ids"])
            assert "text" not in entry
            assert "object_value" not in entry
        # The hot-index entry points at the preference assertion; the "do not
        # repeat" entry points at the negative assertion.
        assert entries[0]["source_assertion_ids"] == ["mas_hot_entry_pref"]
        assert do_not_repeat[0]["source_assertion_ids"] == ["mas_hot_entry_neg"]


def test_topic_context_block_without_topic_id_is_rejected_by_schema_check(
    session_factory: sessionmaker[Session],
) -> None:
    # A topic-type context block must point at a topic; the model CHECK rejects a
    # topic block whose topic_id is null.
    now = datetime(2026, 5, 15, 9, 15, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="ck_memory_context_block_topic_binding"):
        with session_factory() as db:
            with db.begin():
                db.add(
                    MemoryContextBlockRecord(
                        id="mcb_topic_no_topic_id",
                        block_type="topic",
                        scope_key="global",
                        content="topic block without a topic",
                        topic_id=None,
                        lifecycle_state="active",
                        source_assertion_ids=[],
                        source_episode_ids=[],
                        source_trace_ids=[],
                        source_action_trace_ids=[],
                        source_procedure_ids=[],
                        source_project_state_snapshot_ids=[],
                        source_memory_versions={},
                        source_projection_versions={},
                        projection_version=memory_module.MEMORY_PROJECTION_VERSION,
                        created_at=now,
                        updated_at=now,
                    )
                )


def _seed_forgetting_assertion(
    db: Session,
    *,
    assertion_id: str,
    predicate: str,
    text: str,
    confidence: float,
    last_verified_at: datetime,
    now: datetime,
    assertion_type: str = "fact",
    lifecycle_state: str = "active",
    object_value: dict[str, Any] | None = None,
    user_priority: str | None = None,
    salience_score: float = 0.0,
) -> None:
    # An assertion whose confidence and last-verified age are fully controlled,
    # plus an optional salience row, so the FO-3 forgetting pass can be exercised
    # against a known value score.
    entity_id = f"ent_{assertion_id}"
    db.add(
        MemoryEntityRecord(
            id=entity_id,
            entity_type="user",
            entity_key=assertion_id,
            display_name="Forgetting test user",
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
            predicate=predicate,
            scope_key="global",
            object_value=object_value if object_value is not None else {"text": text},
            assertion_type=assertion_type,
            is_multi_valued=True,
            scope={},
            lifecycle_state=lifecycle_state,
            confidence=confidence,
            valid_from=None,
            valid_to=None,
            superseded_by_assertion_id=None,
            extraction_model=None,
            extraction_prompt_version=None,
            last_verified_at=last_verified_at,
            created_at=last_verified_at,
            updated_at=now,
        )
    )
    db.flush()
    if user_priority is not None or salience_score:
        db.add(
            MemorySalienceRecord(
                id=f"msl_{assertion_id}",
                assertion_id=assertion_id,
                user_priority=user_priority or "none",
                score=salience_score,
                signals={},
                created_at=now,
                updated_at=now,
            )
        )


def _consolidate_global(db: Session, *, now: datetime) -> dict[str, Any]:
    return memory_module.consolidate_memory(
        db,
        scope_key="global",
        actor_id="system",
        now_fn=lambda: now,
        new_id_fn=_hot_index_new_id,
        settings=_settings(),
    )


def _hot_index_assertion_ids(db: Session) -> set[str]:
    block = db.scalar(
        select(MemoryContextBlockRecord).where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.scope_key == "global",
        )
    )
    if block is None:
        return set()
    content = json.loads(block.content)
    refs: set[str] = set(block.source_assertion_ids)
    for entry in [*content["entries"], *content["do_not_repeat"]]:
        refs.update(entry["source_assertion_ids"])
    return refs


def test_consolidate_memory_demotes_low_value_long_unverified_assertion(
    session_factory: sessionmaker[Session],
) -> None:
    # A low-confidence assertion of a decaying predicate, unverified far past the
    # staleness horizon, has an effective value below the floor. The FO-3
    # forgetting pass demotes it to `stale` with an audited rationale; it then
    # drops out of the active set and the rebuilt hot index.
    now = datetime(2026, 5, 16, 10, 0, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_forget_stale",
                predicate="project.blocker",
                text="the staging cluster is degraded",
                confidence=0.4,
                last_verified_at=now - timedelta(days=200),
                now=now,
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    demotions = [c for c in result["proposed_changes"] if c["kind"] == "forgetting_demote"]
    assert [c["assertion_id"] for c in demotions] == ["mas_forget_stale"]
    assert demotions[0]["unverified_days"] == 200

    with session_factory() as db:
        assertion = db.get(MemoryAssertionRecord, "mas_forget_stale")
        assert assertion is not None
        # Demotion to `stale` is the whole "forget from recall" mechanism.
        assert assertion.lifecycle_state == "stale"
        assert assertion.invalidated_at == now
        # The demotion rationale is audited on the version row by mark_assertion_stale.
        stale_version = db.scalar(
            select(MemoryVersionRecord).where(
                MemoryVersionRecord.canonical_table == "memory_assertions",
                MemoryVersionRecord.canonical_id == "mas_forget_stale",
                MemoryVersionRecord.change_type == "updated",
            )
        )
        assert stale_version is not None
        assert stale_version.new_state is not None
        assert stale_version.new_state["staleness_reason"].startswith("forgetting policy:")
        # An active-only recall pool, the hot index, and topic blocks all exclude
        # a `stale` assertion, so it has disappeared from recall.
        assert "mas_forget_stale" not in _hot_index_assertion_ids(db)
        assert (
            db.scalar(
                select(func.count())
                .select_from(MemoryAssertionRecord)
                .where(
                    MemoryAssertionRecord.id == "mas_forget_stale",
                    MemoryAssertionRecord.lifecycle_state == "active",
                )
            )
            == 0
        )


def test_consolidate_memory_never_demotes_pinned_assertion(
    session_factory: sessionmaker[Session],
) -> None:
    # A pinned assertion in exactly the demotable condition (low confidence,
    # long unverified, decaying predicate) is never demoted by the forgetting
    # pass, regardless of its value score.
    now = datetime(2026, 5, 16, 10, 5, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_forget_pinned",
                predicate="project.blocker",
                text="the staging cluster is degraded",
                confidence=0.4,
                last_verified_at=now - timedelta(days=200),
                now=now,
                user_priority="pinned",
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    assert [c for c in result["proposed_changes"] if c["kind"] == "forgetting_demote"] == []
    with session_factory() as db:
        assertion = db.get(MemoryAssertionRecord, "mas_forget_pinned")
        assert assertion is not None
        assert assertion.lifecycle_state == "active"
        assert "mas_forget_pinned" in _hot_index_assertion_ids(db)


def test_consolidate_memory_keeps_recently_verified_and_high_salience_assertions(
    session_factory: sessionmaker[Session],
) -> None:
    # The forgetting pass leaves two assertions alone: one verified moments ago
    # (inside the staleness horizon), and one long-unverified and low-confidence
    # but carrying a high salience score that lifts its value above the floor.
    now = datetime(2026, 5, 16, 10, 10, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_forget_recent",
                predicate="project.blocker",
                text="the build pipeline is flaky",
                confidence=0.4,
                last_verified_at=now,
                now=now,
            )
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_forget_salient",
                predicate="project.blocker",
                text="the payments integration is blocked",
                confidence=0.4,
                last_verified_at=now - timedelta(days=200),
                now=now,
                salience_score=0.9,
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    assert [c for c in result["proposed_changes"] if c["kind"] == "forgetting_demote"] == []
    with session_factory() as db:
        for assertion_id in ("mas_forget_recent", "mas_forget_salient"):
            assertion = db.get(MemoryAssertionRecord, assertion_id)
            assert assertion is not None
            assert assertion.lifecycle_state == "active"


def test_consolidate_memory_deletes_assertion_past_delete_after_retention(
    session_factory: sessionmaker[Session],
) -> None:
    # A delete_after retention policy past its day horizon routes the matching
    # assertion to deletion in the forgetting pass; a non-matching assertion is
    # untouched.
    now = datetime(2026, 5, 16, 10, 15, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_retention_match",
                predicate="fact.tooling",
                text="we still call the obsolete vendor api for invoices",
                confidence=0.95,
                last_verified_at=now - timedelta(days=120),
                now=now,
            )
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_retention_other",
                predicate="fact.tooling",
                text="the current billing service handles invoices",
                confidence=0.95,
                last_verified_at=now - timedelta(days=120),
                now=now,
            )
            db.add(
                MemoryRetentionPolicyRecord(
                    id="mrp_delete_after",
                    scope_key="global",
                    policy_kind="delete_after",
                    pattern="obsolete vendor",
                    retention_days=90,
                    lifecycle_state="active",
                    reason="vendor decommissioned",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    retention_deletes = [c for c in result["proposed_changes"] if c["kind"] == "retention_delete"]
    assert [c["assertion_id"] for c in retention_deletes] == ["mas_retention_match"]
    assert retention_deletes[0]["retention_policy_id"] == "mrp_delete_after"

    with session_factory() as db:
        matched = db.get(MemoryAssertionRecord, "mas_retention_match")
        other = db.get(MemoryAssertionRecord, "mas_retention_other")
        assert matched is not None and matched.lifecycle_state == "deleted"
        # delete_after is "forget from recall + existence" via the standard
        # delete path; a deletion audit row is written.
        assert (
            db.scalar(
                select(func.count())
                .select_from(MemoryDeletionRecord)
                .where(
                    MemoryDeletionRecord.target_id == "mas_retention_match",
                    MemoryDeletionRecord.deletion_type == "delete",
                )
            )
            == 1
        )
        # The high-confidence, recently-verified, unmatched assertion survives.
        assert other is not None and other.lifecycle_state == "active"
        assert "mas_retention_match" not in _hot_index_assertion_ids(db)


def test_consolidate_memory_routes_review_after_retention_to_operator_review(
    session_factory: sessionmaker[Session],
) -> None:
    # A review_after retention policy past its horizon routes the matching
    # assertion to operator review; the assertion stays active pending that
    # review and is not demoted by the forgetting pass.
    now = datetime(2026, 5, 16, 10, 20, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_review_after",
                predicate="fact.tooling",
                text="the quarterly access review covers the audit log",
                confidence=0.95,
                last_verified_at=now - timedelta(days=120),
                now=now,
            )
            db.add(
                MemoryRetentionPolicyRecord(
                    id="mrp_review_after",
                    scope_key="global",
                    policy_kind="review_after",
                    pattern="quarterly access review",
                    retention_days=90,
                    lifecycle_state="active",
                    reason="must be re-confirmed quarterly",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    reviews = [c for c in result["proposed_changes"] if c["kind"] == "retention_review"]
    assert [c["assertion_id"] for c in reviews] == ["mas_review_after"]

    with session_factory() as db:
        assertion = db.get(MemoryAssertionRecord, "mas_review_after")
        assert assertion is not None
        assert assertion.lifecycle_state == "active"
        review = db.scalar(
            select(MemoryReviewRecord).where(
                MemoryReviewRecord.assertion_id == "mas_review_after",
                MemoryReviewRecord.decision == "needs_operator_review",
            )
        )
        assert review is not None
        assert review.reason is not None
        assert "review_after" in review.reason


def test_consolidate_memory_forgetting_pass_cannot_resurface_privacy_deleted_memory(
    session_factory: sessionmaker[Session],
) -> None:
    # Privacy-deleted content is strictly separate from "forget from recall": the
    # forgetting pass only ever reads active assertions, so a privacy-deleted row
    # is never selected, never re-activated, and stays redacted. A retention
    # policy whose pattern would otherwise match it changes nothing.
    now = datetime(2026, 5, 16, 10, 25, tzinfo=UTC)
    with session_factory() as db:
        with db.begin():
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_privacy_deleted",
                predicate="fact.contact_detail",
                text="[privacy_deleted]",
                object_value={"text": "[privacy_deleted]"},
                confidence=0.95,
                last_verified_at=now - timedelta(days=400),
                now=now,
                lifecycle_state="privacy_deleted",
            )
            _seed_forgetting_assertion(
                db,
                assertion_id="mas_active_neighbor",
                predicate="fact.tooling",
                text="the team uses the standard deploy script",
                confidence=0.95,
                last_verified_at=now,
                now=now,
            )
            db.add(
                MemoryRetentionPolicyRecord(
                    id="mrp_delete_all",
                    scope_key="global",
                    policy_kind="delete_after",
                    pattern="*",
                    retention_days=30,
                    lifecycle_state="active",
                    reason="aggressive retention",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )

    with session_factory() as db:
        with db.begin():
            result = _consolidate_global(db, now=now)

    touched_privacy = [
        c for c in result["proposed_changes"] if c.get("assertion_id") == "mas_privacy_deleted"
    ]
    assert touched_privacy == []

    with session_factory() as db:
        privacy_deleted = db.get(MemoryAssertionRecord, "mas_privacy_deleted")
        assert privacy_deleted is not None
        # The privacy-deleted row is untouched and provably cannot resurface.
        assert privacy_deleted.lifecycle_state == "privacy_deleted"
        assert privacy_deleted.object_value == {"text": "[privacy_deleted]"}
        assert "mas_privacy_deleted" not in _hot_index_assertion_ids(db)


def _seed_repo_procedure(
    db: Session,
    *,
    suffix: str,
    repo_scope: str,
    title: str,
    instruction: str,
    now: datetime,
) -> str:
    # A repo-scoped, active, reviewed MemoryProcedureRecord backed by a real
    # procedure assertion and evidence, so the FO-5 rules projection can
    # materialise it and a deletion of the assertion can invalidate the
    # artifact through the existing _delete_projection_rows content match.
    # Returns the source assertion id.
    entity_id = f"ent_repo_proc_{suffix}"
    assertion_id = f"mas_repo_proc_{suffix}"
    evidence_id = f"mev_repo_proc_{suffix}"
    session_id = f"ses_repo_proc_{suffix}"
    db.add(
        SessionRecord(
            id=session_id,
            is_active=False,
            lifecycle_state="closed",
            memory_mode="normal",
            rotated_from_session_id=None,
            rotation_reason=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryEntityRecord(
            id=entity_id,
            entity_type="repo",
            entity_key=f"{repo_scope}:{suffix}",
            display_name="Repo rules test repo",
            summary=None,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryEvidenceRecord(
            id=evidence_id,
            source_turn_id=None,
            source_session_id=session_id,
            actor_id="user.local",
            content_class="user_message",
            trust_boundary="trusted_user",
            lifecycle_state="available",
            source_uri=None,
            source_artifact_id=None,
            source_text=instruction,
            evidence_snippet=instruction,
            redaction_posture="none",
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
            subject_key=repo_scope,
            predicate=title,
            scope_key=repo_scope,
            object_value={"text": instruction},
            assertion_type="procedure",
            is_multi_valued=True,
            scope={},
            lifecycle_state="active",
            confidence=0.95,
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
    db.flush()
    db.add(
        MemoryProcedureRecord(
            id=f"mpr_repo_proc_{suffix}",
            procedure_key=f"procedure_{suffix}",
            scope_key=repo_scope,
            title=title,
            instruction=instruction,
            lifecycle_state="active",
            review_state="approved",
            source_assertion_id=assertion_id,
            primary_evidence_id=evidence_id,
            valid_from=now - timedelta(days=1),
            valid_to=None,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    return assertion_id


def _seed_repo_conventions_block(
    db: Session,
    *,
    repo_scope: str,
    summary: str,
    now: datetime,
) -> None:
    # An active repo-conventions topic plus its topic context block, the second
    # source the FO-5 rules projection folds into the AGENTS.md-style file.
    topic_id = "mtp_repo_conventions"
    db.add(
        MemoryTopicRecord(
            id=topic_id,
            topic_key=f"{repo_scope}:repo-conventions",
            family="repo-conventions",
            scope_key=repo_scope,
            title="repo conventions",
            summary=summary,
            lifecycle_state="active",
            projection_version=memory_module.MEMORY_PROJECTION_VERSION,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    db.add(
        MemoryContextBlockRecord(
            id="mcb_repo_conventions",
            block_type="topic",
            scope_key=repo_scope,
            content=summary,
            topic_id=topic_id,
            lifecycle_state="active",
            source_assertion_ids=[],
            source_episode_ids=[],
            source_trace_ids=[],
            source_action_trace_ids=[],
            source_procedure_ids=[],
            source_project_state_snapshot_ids=[],
            source_memory_versions={},
            source_projection_versions={
                "memory_context_blocks": memory_module.MEMORY_PROJECTION_VERSION
            },
            projection_version=memory_module.MEMORY_PROJECTION_VERSION,
            created_at=now,
            updated_at=now,
        )
    )


def test_consolidate_repo_scope_produces_agents_md_artifact_with_rule_source_ids(
    session_factory: sessionmaker[Session],
) -> None:
    # FO-5: consolidating a repo scope materialises its active, reviewed
    # procedural memory plus the repo-conventions topic block into an
    # AGENTS.md-style markdown export artifact. Each rule carries the source
    # memory id it traces back to.
    now = datetime(2026, 5, 16, 11, 0, tzinfo=UTC)
    repo_scope = "repo:ariel"
    with session_factory() as db:
        with db.begin():
            deploy_source = _seed_repo_procedure(
                db,
                suffix="deploy",
                repo_scope=repo_scope,
                title="deploy",
                instruction="Run smoke tests before deploying.",
                now=now,
            )
            test_source = _seed_repo_procedure(
                db,
                suffix="test",
                repo_scope=repo_scope,
                title="test",
                instruction="Run uv run pytest before every push.",
                now=now,
            )
            _seed_repo_conventions_block(
                db,
                repo_scope=repo_scope,
                summary="Flat modules; from __future__ import annotations everywhere.",
                now=now,
            )

    with session_factory() as db:
        with db.begin():
            result = memory_module.consolidate_memory(
                db,
                scope_key=repo_scope,
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    rules_changes = [
        change
        for change in result["applied_projection_changes"]
        if change.get("kind") == "repo_rules_artifact"
    ]
    assert len(rules_changes) == 1
    assert rules_changes[0]["scope_key"] == repo_scope

    with session_factory() as db:
        artifact = db.scalar(
            select(MemoryExportArtifactRecord).where(
                MemoryExportArtifactRecord.scope_key == repo_scope,
                MemoryExportArtifactRecord.artifact_kind == "agents_md",
            )
        )
        assert artifact is not None
        assert artifact.export_format == "markdown"
        assert artifact.status == "created"
        assert artifact.source_counts == {"procedures": 2, "repo_conventions_block": 1}
        markdown = artifact.content["markdown"]
        # The AGENTS.md-style file is organised in sections and folds in the
        # repo-conventions topic-block content.
        assert "## Repo Conventions" in markdown
        assert "## Procedural Rules" in markdown
        assert "from __future__ import annotations everywhere" in markdown
        # Every rule names its source memory id so it traces back to canonical
        # memory; the ids are the procedures' source assertions.
        rule_sources = {rule["source_memory_id"] for rule in artifact.content["rules"]}
        assert rule_sources == {deploy_source, test_source}
        for rule in artifact.content["rules"]:
            assert f"[source: {rule['source_memory_id']}]" in markdown
        # Procedure instructions appear one rule per item.
        assert "Run smoke tests before deploying." in markdown
        assert "Run uv run pytest before every push." in markdown


def test_repo_rules_artifact_is_a_refreshed_projection_not_canonical_state(
    session_factory: sessionmaker[Session],
) -> None:
    # FO-5: the rules file is a projection. A regenerating consolidation
    # refreshes the same artifact row in place, and editing the artifact
    # content never mutates canonical memory -- only consolidation rebuilds it.
    first = datetime(2026, 5, 16, 11, 5, tzinfo=UTC)
    second = datetime(2026, 5, 16, 12, 5, tzinfo=UTC)
    repo_scope = "repo:atlas"
    with session_factory() as db:
        with db.begin():
            _seed_repo_procedure(
                db,
                suffix="deploy",
                repo_scope=repo_scope,
                title="deploy",
                instruction="Run smoke tests before deploying.",
                now=first,
            )

    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key=repo_scope,
                actor_id="system",
                now_fn=lambda: first,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    with session_factory() as db:
        with db.begin():
            artifact = db.scalar(
                select(MemoryExportArtifactRecord).where(
                    MemoryExportArtifactRecord.artifact_kind == "agents_md"
                )
            )
            assert artifact is not None
            artifact_id = artifact.id
            # Simulate an out-of-band edit of the projection file.
            artifact.content = {"markdown": "hand-edited", "rules": []}

    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key=repo_scope,
                actor_id="system",
                now_fn=lambda: second,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    with session_factory() as db:
        artifacts = db.scalars(
            select(MemoryExportArtifactRecord).where(
                MemoryExportArtifactRecord.artifact_kind == "agents_md"
            )
        ).all()
        # The regenerating consolidation refreshed the same row, not a new one.
        assert [artifact.id for artifact in artifacts] == [artifact_id]
        refreshed = artifacts[0]
        assert refreshed.content["markdown"] != "hand-edited"
        assert "Run smoke tests before deploying." in refreshed.content["markdown"]
        assert refreshed.updated_at == second
        # The hand edit did not flow back to canonical memory: the procedure
        # and its source assertion are untouched.
        procedure = db.scalar(
            select(MemoryProcedureRecord).where(MemoryProcedureRecord.scope_key == repo_scope)
        )
        assert procedure is not None
        assert procedure.lifecycle_state == "active"
        assert procedure.instruction == "Run smoke tests before deploying."


def test_deleting_source_procedure_invalidates_repo_rules_artifact(
    session_factory: sessionmaker[Session],
) -> None:
    # FO-5: deleting the procedural memory behind a rule invalidates the
    # AGENTS.md-style artifact through the existing projection-invalidation
    # path -- _delete_projection_rows marks the artifact failed because the
    # deleted source memory id is in its content.
    now = datetime(2026, 5, 16, 11, 10, tzinfo=UTC)
    repo_scope = "repo:phoenix"
    with session_factory() as db:
        with db.begin():
            source_assertion_id = _seed_repo_procedure(
                db,
                suffix="deploy",
                repo_scope=repo_scope,
                title="deploy",
                instruction="Run smoke tests before deploying.",
                now=now,
            )

    with session_factory() as db:
        with db.begin():
            memory_module.consolidate_memory(
                db,
                scope_key=repo_scope,
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
                settings=_settings(),
            )

    with session_factory() as db:
        artifact = db.scalar(
            select(MemoryExportArtifactRecord).where(
                MemoryExportArtifactRecord.artifact_kind == "agents_md"
            )
        )
        assert artifact is not None
        assert artifact.status == "created"

    with session_factory() as db:
        with db.begin():
            events = memory_module.delete_assertion(
                db,
                assertion_id=source_assertion_id,
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_hot_index_new_id,
            )
    assert any(event["event_type"] == "evt.memory.assertion_deleted" for event in events)

    with session_factory() as db:
        artifact = db.scalar(
            select(MemoryExportArtifactRecord).where(
                MemoryExportArtifactRecord.artifact_kind == "agents_md"
            )
        )
        assert artifact is not None
        # The deletion invalidated the projection.
        assert artifact.status == "failed"
        assert artifact.source_counts["invalidated_by_assertion_id"] == source_assertion_id
