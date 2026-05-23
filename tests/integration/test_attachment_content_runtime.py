from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ariel.attachment_content import AttachmentContentRuntime, AttachmentScannerMode
from ariel.ids import new_id
from ariel.persistence import SessionRecord, TurnRecord


NOW = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
SESSION_ID = "ses_attachment_runtime"
TURN_ID = "trn_attachment_runtime"
ATTACHMENT_REF = "discord:131415"
DOWNLOAD_URL = "https://cdn.discordapp.com/attachments/report.txt"


def _runtime(tmp_path: Path, *, scanner_mode: AttachmentScannerMode) -> AttachmentContentRuntime:
    return AttachmentContentRuntime(
        blob_store_path=str(tmp_path / "attachments"),
        max_bytes=25 * 1024 * 1024,
        fetch_timeout_seconds=10.0,
        handle_ttl_seconds=86_400,
        scanner_mode=scanner_mode,
        openai_api_key=None,
        openai_model="gpt-5.5",
        openai_audio_model="gpt-4o-transcribe",
        openai_timeout_seconds=30.0,
        encryption_secret="test-attachment-secret",
        encryption_key_version="v1",
        encryption_keys=None,
    )


def test_attachment_runtime_rejects_unknown_scanner_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scanner_mode must be one of: disabled, fail_closed"):
        AttachmentContentRuntime(
            blob_store_path=str(tmp_path / "attachments"),
            max_bytes=25 * 1024 * 1024,
            fetch_timeout_seconds=10.0,
            handle_ttl_seconds=86_400,
            scanner_mode=cast(Any, "permissive"),
            openai_api_key=None,
            openai_model="gpt-5.5",
            openai_audio_model="gpt-4o-transcribe",
            openai_timeout_seconds=30.0,
            encryption_secret="test-attachment-secret",
            encryption_key_version="v1",
            encryption_keys=None,
        )


def _patch_discord_download(monkeypatch: pytest.MonkeyPatch, *, body: bytes) -> None:
    class FakeStreamResponse:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"content-length": str(len(body))}

        def __enter__(self) -> FakeStreamResponse:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [body]

    class FakeHttpClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            return None

        def __enter__(self) -> FakeHttpClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def stream(self, method: str, request_url: str) -> FakeStreamResponse:
            assert method == "GET"
            assert request_url == DOWNLOAD_URL
            return FakeStreamResponse()

    monkeypatch.setattr("ariel.attachment_content.httpx.Client", FakeHttpClient)


def _seed_turn_and_source(db: Session, runtime: AttachmentContentRuntime) -> None:
    db.add(
        SessionRecord(
            id=SESSION_ID,
            is_active=True,
            lifecycle_state="active",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.add(
        TurnRecord(
            id=TURN_ID,
            session_id=SESSION_ID,
            user_message="please summarize this",
            assistant_message=None,
            status="in_progress",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.flush()
    runtime.record_discord_sources(
        db=db,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        discord_context={
            "message_id": 789,
            "channel_id": 456,
            "author_id": 101112,
            "guild_id": 123,
        },
        attachment_sources=[
            {
                "source": "discord",
                "source_attachment_id": 131415,
                "filename": "report.txt",
                "content_type": "text/plain",
                "size_bytes": 28,
                "attachment_ref": ATTACHMENT_REF,
                "download_url": DOWNLOAD_URL,
            }
        ],
        now_fn=lambda: NOW,
        new_id_fn=new_id,
    )


def _execute_read(
    db: Session,
    runtime: AttachmentContentRuntime,
    *,
    now: datetime,
) -> dict[str, Any]:
    result = runtime.execute_read(
        db=db,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        normalized_input={"attachment_ref": ATTACHMENT_REF, "intent": "summarize"},
        now_fn=lambda: now,
        new_id_fn=new_id,
    )
    assert result.status == "succeeded"
    assert result.output is not None
    return result.output


@pytest.mark.parametrize(
    ("body", "expected_status", "stale_recovery_terms"),
    [
        (b"quarterly revenue increased", "scan_failed", ("configure", "scanning")),
        (
            b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
            "unsafe",
            ("safety", "scanning"),
        ),
    ],
)
def test_attachment_read_returns_scanner_gate_failures_without_persisting_content(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
    expected_status: str,
    stale_recovery_terms: tuple[str, str],
) -> None:
    runtime = _runtime(tmp_path, scanner_mode="fail_closed")
    _patch_discord_download(monkeypatch, body=body)

    with session_factory() as db:
        with db.begin():
            _seed_turn_and_source(db, runtime)
            output = _execute_read(db, runtime, now=NOW)

            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()

    read_outcome = output["read_outcome"]
    assert read_outcome["status"] == expected_status
    assert read_outcome["reason_code"] == expected_status
    assert isinstance(read_outcome["recovery"], str)
    assert read_outcome["recovery"].strip()
    assert not all(term in read_outcome["recovery"] for term in stale_recovery_terms)
    assert output["blocks"] == []
    assert output["results"] == []
    assert persisted_content_counts == (0, 0)
    assert not (tmp_path / "attachments").exists()


def test_attachment_read_rehydrates_deleted_blob_before_extraction(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, scanner_mode="disabled")
    _patch_discord_download(monkeypatch, body=b"quarterly revenue increased")

    with session_factory() as db:
        with db.begin():
            _seed_turn_and_source(db, runtime)
            _execute_read(db, runtime, now=NOW)
            blob_id, storage_key = db.execute(
                text("SELECT id, storage_key FROM attachment_blobs")
            ).one()
            deleted_at = NOW + timedelta(minutes=5)
            db.execute(
                text(
                    "UPDATE attachment_blobs "
                    "SET deleted_at = :deleted_at, updated_at = :deleted_at "
                    "WHERE id = :blob_id"
                ),
                {"blob_id": blob_id, "deleted_at": deleted_at},
            )
            db.execute(
                text(
                    "UPDATE attachment_extractions "
                    "SET blocks = CAST(:blocks AS jsonb), updated_at = :updated_at "
                    "WHERE blob_id = :blob_id"
                ),
                {
                    "blob_id": blob_id,
                    "blocks": json.dumps([{"kind": "text", "text": "cached deleted secret"}]),
                    "updated_at": NOW + timedelta(minutes=30),
                },
            )

    with session_factory() as db:
        with db.begin():
            output = _execute_read(db, runtime, now=NOW + timedelta(minutes=10))
            blob_counts = db.execute(
                text(
                    "SELECT count(*) AS blobs, count(*) FILTER (WHERE deleted_at IS NULL) "
                    "AS live_blobs FROM attachment_blobs"
                )
            ).one()
            extraction_count = db.execute(
                text("SELECT count(*) FROM attachment_extractions")
            ).scalar_one()

    assert output["read_outcome"]["status"] == "ok"
    assert output["blocks"] == [{"kind": "text", "text": "quarterly revenue increased"}]
    assert "cached deleted secret" not in json.dumps(output, sort_keys=True)
    assert (tmp_path / "attachments" / storage_key).is_file()
    assert blob_counts == (1, 1)
    assert extraction_count == 2


def test_attachment_read_missing_cached_blob_file_rechecks_scanner_mode(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, scanner_mode="disabled")
    _patch_discord_download(monkeypatch, body=b"quarterly revenue increased")

    with session_factory() as db:
        with db.begin():
            _seed_turn_and_source(db, runtime)
            _execute_read(db, runtime, now=NOW)
            storage_key = db.execute(text("SELECT storage_key FROM attachment_blobs")).scalar_one()
    (tmp_path / "attachments" / storage_key).unlink()

    with session_factory() as db:
        with db.begin():
            output = _execute_read(
                db,
                replace(runtime, scanner_mode="fail_closed"),
                now=NOW + timedelta(minutes=10),
            )
            persisted_content_counts = db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM attachment_blobs) AS blobs, "
                    "(SELECT count(*) FROM attachment_extractions) AS extractions"
                )
            ).one()

    assert output["read_outcome"]["status"] == "scan_failed"
    assert output["blocks"] == []
    assert output["results"] == []
    assert persisted_content_counts == (1, 1)
