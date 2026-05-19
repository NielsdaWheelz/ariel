"""The single writer for the ``ai_judgments`` audit log.

Every bounded AI subagent call -- the memory retriever and rememberer, and the
main agent turn's model output -- records exactly one ``ai_judgments`` row
through :func:`record_ai_judgment`. There is no other writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from .persistence import AIJudgmentRecord


class AIJudgmentFailure(RuntimeError):
    """A bounded AI subagent call that failed -- network error, malformed
    output, or a schema/validation violation. Carries the parse, validation,
    and failure-code fields that the failed ``ai_judgments`` row records."""

    def __init__(
        self,
        *,
        code: str,
        safe_reason: str,
        retryable: bool,
        parse_status: str,
        validation_status: str,
        provider_response_id: str | None = None,
    ) -> None:
        super().__init__(safe_reason)
        self.code = code
        self.safe_reason = safe_reason
        self.retryable = retryable
        self.parse_status = parse_status
        self.validation_status = validation_status
        self.provider_response_id = provider_response_id


def record_ai_judgment(
    db: Session,
    *,
    judgment_type: Literal[
        "memory_recall", "memory_encode", "memory_dream", "model_output", "research"
    ],
    source_type: str,
    source_id: str,
    model: str | None,
    prompt_version: str,
    provider_response_id: str | None,
    input_summary: str,
    input_refs: Mapping[str, Any],
    output: Mapping[str, Any],
    now: datetime,
    new_id: Callable[[str], str],
    failure: AIJudgmentFailure | None = None,
) -> None:
    """Add the one ``ai_judgments`` row auditing a bounded AI subagent call.

    ``failure`` is ``None`` on success; when set, it carries the parse and
    validation status of the failed call. The row joins the caller's
    transaction -- this does not flush or commit.
    """
    db.add(
        AIJudgmentRecord(
            id=new_id("ajg"),
            judgment_type=judgment_type,
            source_type=source_type,
            source_id=source_id,
            status="failed" if failure is not None else "succeeded",
            model=model,
            prompt_version=prompt_version,
            provider_response_id=provider_response_id,
            input_summary=input_summary,
            input_refs=jsonable_encoder(input_refs),
            output=jsonable_encoder(output),
            parse_status=failure.parse_status if failure is not None else "parsed",
            validation_status=(failure.validation_status if failure is not None else "valid"),
            failure_code=failure.code if failure is not None else None,
            failure_reason=failure.safe_reason if failure is not None else None,
            created_at=now,
        )
    )
