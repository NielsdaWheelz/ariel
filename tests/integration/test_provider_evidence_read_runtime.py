from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import RuntimeProvenance
from ariel.config import AppSettings
from ariel.persistence import (
    GoogleProviderObjectRecord,
    ProviderEvidenceBlockRecord,
    ProviderEvidenceRecord,
    TurnRecord,
)
from ariel.run_runtime import execute_run_program
from tests.fake_sandbox import FakeSandboxRuntime

NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _settings() -> AppSettings:
    from typing import cast

    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None))


def test_provider_evidence_read_returns_text_to_program_and_redacts_audit(
    session_factory: sessionmaker[Session],
) -> None:
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    events: list[tuple[str, dict[str, Any]]] = []
    try:
        with session_factory() as db:
            with db.begin():
                turn = TurnRecord(
                    id="trn_provider_evidence_read",
                    user_message="read evidence",
                    assistant_message=None,
                    status="in_progress",
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add(turn)
                db.add(
                    GoogleProviderObjectRecord(
                        id="gpo_read_1",
                        provider_account_id="acct_1",
                        object_type="gmail_message",
                        external_id="msg_1",
                        thread_external_id="thr_1",
                        calendar_id=None,
                        ical_uid=None,
                        status="active",
                        source_timestamp=NOW,
                        observed_at=NOW,
                        provider_url=None,
                        metadata_json={},
                        content_digest="d" * 64,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.add(
                    ProviderEvidenceRecord(
                        id="pev_read_1",
                        provider_object_id="gpo_read_1",
                        provider="google",
                        provider_account_id="acct_1",
                        source_kind="gmail_message",
                        external_id="msg_1",
                        thread_external_id="thr_1",
                        calendar_id=None,
                        source_uri=None,
                        source_timestamp=NOW,
                        content_digest="d" * 64,
                        metadata_json={},
                        taint="provider_untrusted",
                        sensitivity="private",
                        retention_policy="provider_source",
                        extraction_status="pending",
                        lifecycle_state="available",
                        observed_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                db.flush()
                db.add(
                    ProviderEvidenceBlockRecord(
                        id="peb_read_1",
                        evidence_id="pev_read_1",
                        block_index=0,
                        block_kind="body",
                        text="Full schedule includes stack 02 after the preview.",
                        digest="e" * 64,
                        source_offsets={"block_id": "gmail:msg_1:body:0"},
                        metadata_json={"truncated": False},
                        created_at=NOW,
                    )
                )
                db.flush()
                result = execute_run_program(
                    sandbox=sandbox,
                    source=(
                        "result = provider_evidence.read(provider_evidence_id='pev_read_1')\n"
                        "agent.emit_value(value={\n"
                        "    'status': result['read_outcome']['status'],\n"
                        "    'text': result['blocks'][0]['text'],\n"
                        "})\n"
                    ),
                    db=db,
                    session_factory=session_factory,
                    turn=turn,
                    proposal_index_start=0,
                    approval_ttl_seconds=300,
                    approval_actor_id="user:test",
                    add_event=lambda event_type, payload: events.append((event_type, payload)),
                    now_fn=lambda: NOW,
                    new_id_fn=lambda prefix: f"{prefix}_read",
                    runtime_provenance=RuntimeProvenance(status="clean"),
                    google_runtime=None,
                    execute_google_reads_outside_transaction=False,
                    agency_runtime=None,
                    attachment_runtime=None,
                    allowed_capability_ids={"cap.provider_evidence.read"},
                    settings=_settings(),
                    scratch={},
                )
    finally:
        sandbox.close()

    assert result.program_ok is True, result.program_error
    assert result.emitted_values == [
        {
            "status": "ok",
            "text": "Full schedule includes stack 02 after the preview.",
        }
    ]
    attempt = result.action_attempts[0]
    assert attempt.capability_id == "cap.provider_evidence.read"
    assert attempt.execution_output is not None
    assert "text" not in attempt.execution_output["blocks"][0]
    assert attempt.execution_output["blocks"][0]["text_redacted"] is True
    succeeded = [
        payload for event_type, payload in events if event_type == "evt.action.execution.succeeded"
    ]
    assert succeeded
    assert succeeded[0]["capability_id"] == "cap.provider_evidence.read"
    assert succeeded[0]["status"] == "succeeded"
    assert "text" not in succeeded[0]["execution_output"]["blocks"][0]
    assert result.runtime_provenance is not None
    assert result.runtime_provenance.status == "tainted"
