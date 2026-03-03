from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Literal, Protocol

import httpx
import ulid
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    create_engine,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ariel.action_runtime import (
    ActionRuntimeError,
    RuntimeProvenance,
    process_action_proposals,
    resolve_approval_decision,
)
from ariel.config import AppSettings
from ariel.db import missing_required_tables, reset_schema_for_tests
from ariel.persistence import (
    ActionAttemptRecord,
    ApprovalRequestRecord,
    EventRecord,
    SessionRecord,
    TurnRecord,
    serialize_action_attempt,
    serialize_session,
    serialize_turn,
    to_rfc3339,
)
from ariel.phone_surface import PHONE_SURFACE_HTML
from ariel.redaction import redact_text, safe_failure_reason


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


_ACTIVE_SESSION_LOCK_ID = 24_310_001

_CONTEXT_SECTION_ORDER = (
    "policy_system_instructions",
    "recent_active_session_turns",
)

_CONTEXT_AUDIT_SCHEMA_VERSION = "1.0"

_POLICY_SYSTEM_INSTRUCTIONS = (
    "You are Ariel, a private assistant for one active user session.",
    "If user intent is clear, answer directly in this turn.",
    "If user intent is ambiguous or conflicting, ask for the missing details instead of guessing.",
    "If the user asks about details not present in this context, state uncertainty and ask for recovery details.",
)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)

    @field_validator("message")
    @classmethod
    def _message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ApprovalDecisionRequest(BaseModel):
    approval_ref: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "deny"]
    actor_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("approval_ref")
    @classmethod
    def _approval_ref_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("approval_ref must not be blank")
        return normalized

    @field_validator("actor_id")
    @classmethod
    def _normalize_actor_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ModelAdapter(Protocol):
    provider: str
    model: str

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class EchoModelAdapter:
    provider: str = "provider.local"
    model: str = "echo-v1"

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history, context_bundle
        prompt_tokens = max(1, len(user_message.split()))
        completion_text = f"echo: {user_message}"
        completion_tokens = max(1, len(completion_text.split()))
        return {
            "assistant_text": completion_text,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "provider_response_id": _new_id("resp"),
        }


class ModelAdapterError(Exception):
    def __init__(
        self,
        *,
        safe_reason: str,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(slots=True)
class OpenAIChatCompletionsAdapter:
    provider: str
    model: str
    api_base_url: str
    api_key: str | None
    timeout_seconds: float = 30.0

    def respond(
        self,
        user_message: str,
        *,
        session_id: str,
        turn_id: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del session_id, turn_id, history
        if not self.api_key:
            raise ModelAdapterError(
                safe_reason="model credentials are not configured",
                status_code=503,
                code="E_MODEL_CREDENTIALS",
                message="model credentials are not configured",
                retryable=False,
            )

        try:
            response = httpx.post(
                f"{self.api_base_url.rstrip('/')}/chat/completions",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": _build_openai_messages(
                        context_bundle=context_bundle,
                        user_message=user_message,
                    ),
                },
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelAdapterError(
                safe_reason="model provider request timed out",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelAdapterError(
                safe_reason="model provider network request failed",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc

        if response.status_code in {401, 403}:
            raise ModelAdapterError(
                safe_reason="model credentials were rejected by provider",
                status_code=502,
                code="E_MODEL_CREDENTIALS",
                message="model credentials were rejected by provider",
                retryable=False,
            )

        if response.status_code >= 400:
            raise ModelAdapterError(
                safe_reason=f"model provider returned HTTP {response.status_code}",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelAdapterError(
                safe_reason="model provider returned invalid JSON",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            ) from exc

        assistant_text = _extract_openai_assistant_text(payload)
        if not assistant_text:
            raise ModelAdapterError(
                safe_reason="model provider returned empty assistant response",
                status_code=502,
                code="E_MODEL_FAILURE",
                message="model provider request failed",
                retryable=True,
            )

        usage_payload = payload.get("usage")
        usage = usage_payload if isinstance(usage_payload, dict) else None
        provider_response_id = payload.get("id")

        return {
            "assistant_text": assistant_text,
            "provider": self.provider,
            "model": self.model,
            "usage": usage,
            "provider_response_id": provider_response_id,
        }


def _build_openai_messages(
    *,
    context_bundle: dict[str, Any],
    user_message: str,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    policy_system_instructions = context_bundle.get("policy_system_instructions")
    if isinstance(policy_system_instructions, list):
        for instruction in policy_system_instructions:
            if isinstance(instruction, str) and instruction:
                messages.append({"role": "system", "content": instruction})

    recent_turns = context_bundle.get("recent_active_session_turns")
    if not isinstance(recent_turns, list):
        recent_turns = []

    for prior_turn in recent_turns:
        if not isinstance(prior_turn, dict):
            continue
        prior_user_message = prior_turn.get("user_message")
        if isinstance(prior_user_message, str) and prior_user_message:
            messages.append({"role": "user", "content": prior_user_message})
        prior_assistant_message = prior_turn.get("assistant_message")
        if isinstance(prior_assistant_message, str) and prior_assistant_message:
            messages.append({"role": "assistant", "content": prior_assistant_message})
    messages.append({"role": "user", "content": user_message})
    return messages


def _extract_openai_assistant_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message_payload = first_choice.get("message")
    if not isinstance(message_payload, dict):
        return ""
    content = message_payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        return "".join(text_parts).strip()
    return ""


@dataclass(slots=True, frozen=True)
class TurnLimitViolation:
    budget: str
    unit: str
    measured: int
    limit: int


def _estimate_text_tokens(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _response_tokens_from_model_payload(
    assistant_response: dict[str, Any],
    *,
    assistant_text: str,
) -> int:
    usage_payload = assistant_response.get("usage")
    if isinstance(usage_payload, dict):
        completion_tokens = usage_payload.get("completion_tokens")
        if isinstance(completion_tokens, int) and completion_tokens >= 0:
            return completion_tokens
    return _estimate_text_tokens(assistant_text)


def _estimate_context_tokens(*, context_bundle: dict[str, Any], user_message: str) -> int:
    token_total = _estimate_text_tokens(user_message)

    policy_system_instructions = context_bundle.get("policy_system_instructions")
    if isinstance(policy_system_instructions, list):
        for instruction in policy_system_instructions:
            if isinstance(instruction, str):
                token_total += _estimate_text_tokens(instruction)

    recent_active_session_turns = context_bundle.get("recent_active_session_turns")
    if isinstance(recent_active_session_turns, list):
        for prior_turn in recent_active_session_turns:
            if not isinstance(prior_turn, dict):
                continue
            prior_user_message = prior_turn.get("user_message")
            if isinstance(prior_user_message, str):
                token_total += _estimate_text_tokens(prior_user_message)
            prior_assistant_message = prior_turn.get("assistant_message")
            if isinstance(prior_assistant_message, str):
                token_total += _estimate_text_tokens(prior_assistant_message)

    return token_total


def _turn_limit_message(violation: TurnLimitViolation) -> str:
    if violation.budget == "context_tokens":
        return (
            "this turn stopped because the context budget was exhausted. "
            "please resend with only the most relevant details needed to proceed."
        )
    if violation.budget == "response_tokens":
        return (
            "this turn stopped because the response budget was exhausted. "
            "please narrow the request so i can answer within the response budget."
        )
    if violation.budget == "model_attempts":
        return (
            "this turn stopped because the model attempt limit was exhausted. "
            "please provide missing details or retry with a narrower request."
        )
    if violation.budget == "turn_wall_time_ms":
        return (
            "this turn stopped because the turn time budget was exhausted. "
            "please split the request into smaller steps so i can complete it."
        )
    return "this turn stopped because a configured turn budget was exhausted."


def _applied_turn_limits(app: FastAPI) -> dict[str, int]:
    return {
        "max_recent_turns": int(app.state.max_recent_turns),
        "max_context_tokens": int(app.state.max_context_tokens),
        "max_response_tokens": int(app.state.max_response_tokens),
        "max_model_attempts": int(app.state.max_model_attempts),
        "max_turn_wall_time_ms": int(app.state.max_turn_wall_time_ms),
    }


def _build_turn_limit_error(
    *,
    session_id: str,
    turn_id: str,
    violation: TurnLimitViolation,
    applied_limits: dict[str, int],
) -> ApiError:
    return ApiError(
        status_code=429,
        code="E_TURN_LIMIT_REACHED",
        message=_turn_limit_message(violation),
        details={
            "session_id": session_id,
            "turn_id": turn_id,
            "limit": {
                "budget": violation.budget,
                "unit": violation.unit,
                "limit": violation.limit,
                "measured": violation.measured,
            },
            "applied_limits": applied_limits,
        },
        retryable=False,
    )


def _build_default_model_adapter(settings: AppSettings) -> ModelAdapter:
    if settings.model_provider == "echo":
        return EchoModelAdapter(model=settings.model_name)
    if settings.model_provider == "openai":
        return OpenAIChatCompletionsAdapter(
            provider="provider.openai",
            model=settings.model_name,
            api_base_url=settings.model_api_base_url,
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
        )
    msg = f"unsupported model provider: {settings.model_provider}"
    raise RuntimeError(msg)


@dataclass(slots=True)
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any]
    retryable: bool = False


def _error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "retryable": error.retryable,
            },
        },
    )


def _build_turn_context_bundle(
    *,
    prior_turns: Sequence[TurnRecord],
    max_recent_turns: int,
) -> dict[str, Any]:
    recent_turns = prior_turns[-max_recent_turns:]
    recent_active_session_turns = [
        {
            "turn_id": turn.id,
            "user_message": turn.user_message,
            "assistant_message": turn.assistant_message,
            "status": turn.status,
        }
        for turn in recent_turns
    ]
    omitted_turn_count = len(prior_turns) - len(recent_active_session_turns)
    included_turn_ids = [turn["turn_id"] for turn in recent_active_session_turns]

    return {
        "section_order": list(_CONTEXT_SECTION_ORDER),
        "policy_system_instructions": list(_POLICY_SYSTEM_INSTRUCTIONS),
        "recent_active_session_turns": recent_active_session_turns,
        "recent_window": {
            "max_recent_turns": max_recent_turns,
            "included_turn_count": len(recent_active_session_turns),
            "omitted_turn_count": omitted_turn_count,
            "included_turn_ids": included_turn_ids,
        },
    }


def _context_bundle_audit_metadata(context_bundle: dict[str, Any]) -> dict[str, Any]:
    section_order_raw = context_bundle.get("section_order")
    section_order = (
        [entry for entry in section_order_raw if isinstance(entry, str)]
        if isinstance(section_order_raw, list)
        else []
    )

    policy_system_instructions_raw = context_bundle.get("policy_system_instructions")
    policy_system_instructions = (
        [entry for entry in policy_system_instructions_raw if isinstance(entry, str)]
        if isinstance(policy_system_instructions_raw, list)
        else []
    )

    recent_window_raw = context_bundle.get("recent_window")
    recent_window = recent_window_raw if isinstance(recent_window_raw, dict) else {}
    max_recent_turns = recent_window.get("max_recent_turns")
    included_turn_count = recent_window.get("included_turn_count")
    omitted_turn_count = recent_window.get("omitted_turn_count")
    included_turn_ids_raw = recent_window.get("included_turn_ids")
    included_turn_ids = (
        [turn_id for turn_id in included_turn_ids_raw if isinstance(turn_id, str)]
        if isinstance(included_turn_ids_raw, list)
        else []
    )

    return {
        "schema_version": _CONTEXT_AUDIT_SCHEMA_VERSION,
        "section_order": section_order,
        "policy_instruction_count": len(policy_system_instructions),
        "recent_window": {
            "max_recent_turns": max_recent_turns if isinstance(max_recent_turns, int) else 0,
            "included_turn_count": included_turn_count if isinstance(included_turn_count, int) else 0,
            "omitted_turn_count": omitted_turn_count if isinstance(omitted_turn_count, int) else 0,
            "included_turn_ids": included_turn_ids,
        },
    }


def _runtime_provenance_for_turn(
    *,
    db: Session,
    prior_turns: Sequence[TurnRecord],
    max_recent_turns: int,
) -> RuntimeProvenance:
    recent_turns = prior_turns[-max_recent_turns:]
    recent_turn_ids = [turn.id for turn in recent_turns]
    if not recent_turn_ids:
        return RuntimeProvenance(status="clean", evidence=())
    attempts_by_turn: dict[str, list[ActionAttemptRecord]] = {turn_id: [] for turn_id in recent_turn_ids}
    for action_attempt in db.scalars(
        select(ActionAttemptRecord)
        .where(
            ActionAttemptRecord.turn_id.in_(recent_turn_ids),
            ActionAttemptRecord.policy_decision == "allow_inline",
            ActionAttemptRecord.status == "succeeded",
        )
        .order_by(
            ActionAttemptRecord.created_at.asc(),
            ActionAttemptRecord.proposal_index.asc(),
            ActionAttemptRecord.id.asc(),
        )
    ).all():
        if action_attempt.turn_id in attempts_by_turn:
            attempts_by_turn[action_attempt.turn_id].append(action_attempt)

    evidence: list[dict[str, Any]] = []
    for turn_id in recent_turn_ids:
        for action_attempt in attempts_by_turn[turn_id]:
            evidence.append(
                {
                    "kind": "prior_tool_output_in_context",
                    "turn_id": turn_id,
                    "action_attempt_id": action_attempt.id,
                    "capability_id": action_attempt.capability_id,
                    "impact_level": action_attempt.impact_level,
                }
            )
    status: Literal["clean", "tainted"] = "tainted" if evidence else "clean"
    return RuntimeProvenance(status=status, evidence=tuple(evidence))


def _get_or_create_active_session(db: Session) -> SessionRecord:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _ACTIVE_SESSION_LOCK_ID},
        )

    active_session = db.scalar(
        select(SessionRecord).where(SessionRecord.is_active.is_(True)).limit(1)
    )
    if active_session:
        return active_session

    now = _utcnow()
    with db.begin_nested():
        created = SessionRecord(
            id=_new_id("ses"),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(created)
        try:
            db.flush()
            return created
        except IntegrityError:
            pass

    active_session = db.scalar(
        select(SessionRecord).where(SessionRecord.is_active.is_(True)).limit(1)
    )
    if active_session is None:
        raise RuntimeError("failed to create or load active session")
    return active_session


def create_app(
    *,
    database_url: str | None = None,
    model_adapter: ModelAdapter | None = None,
    reset_database: bool = False,
) -> FastAPI:
    settings = AppSettings()
    db_url = database_url or settings.database_url
    adapter = model_adapter or _build_default_model_adapter(settings)

    engine = create_engine(db_url, future=True, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if reset_database:
            reset_schema_for_tests(engine, db_url)
        app.state.schema_missing_tables = missing_required_tables(engine)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Ariel Slice 0", lifespan=lifespan)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.model_adapter = adapter
    app.state.bind_host = settings.bind_host
    app.state.bind_port = settings.bind_port
    app.state.max_recent_turns = settings.max_recent_turns
    app.state.max_context_tokens = settings.max_context_tokens
    app.state.max_response_tokens = settings.max_response_tokens
    app.state.max_model_attempts = settings.max_model_attempts
    app.state.max_turn_wall_time_ms = settings.max_turn_wall_time_ms
    app.state.approval_ttl_seconds = settings.approval_ttl_seconds
    app.state.approval_actor_id = settings.approval_actor_id
    app.state.schema_missing_tables = []

    @app.exception_handler(ApiError)
    def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            ApiError(
                status_code=422,
                code="E_VALIDATION",
                message="request validation failed",
                details={"errors": jsonable_encoder(exc.errors())},
                retryable=False,
            )
        )

    @app.exception_handler(Exception)
    def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            ApiError(
                status_code=500,
                code="E_INTERNAL",
                message="internal server error",
                details={"exception_type": exc.__class__.__name__},
                retryable=False,
            )
        )

    @app.get("/", response_class=HTMLResponse)
    def phone_surface() -> str:
        return PHONE_SURFACE_HTML

    def _ensure_schema_ready() -> None:
        if app.state.schema_missing_tables:
            raise ApiError(
                status_code=503,
                code="E_SCHEMA_NOT_READY",
                message="database schema is not migrated",
                details={"missing_tables": app.state.schema_missing_tables},
                retryable=False,
            )

    @app.get("/v1/health", response_model=None)
    def health() -> JSONResponse | dict[str, bool]:
        if app.state.schema_missing_tables:
            return _error_response(
                ApiError(
                    status_code=503,
                    code="E_SCHEMA_NOT_READY",
                    message="database schema is not migrated",
                    details={"missing_tables": app.state.schema_missing_tables},
                    retryable=False,
                )
            )
        return {"ok": True}

    @app.post("/v1/sessions")
    def create_or_get_session() -> dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            with db.begin():
                active_session = _get_or_create_active_session(db)
            return {"ok": True, "session": serialize_session(active_session)}

    @app.get("/v1/sessions/active")
    def get_active_session() -> dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            with db.begin():
                active_session = _get_or_create_active_session(db)
            return {"ok": True, "session": serialize_session(active_session)}

    @app.post("/v1/sessions/{session_id}/message", response_model=None)
    def post_message(session_id: str, payload: MessageRequest) -> JSONResponse | dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            with db.begin():
                active_session = db.scalar(
                    select(SessionRecord)
                    .where(SessionRecord.id == session_id, SessionRecord.is_active.is_(True))
                    .limit(1)
                )
                if active_session is None:
                    raise ApiError(
                        status_code=404,
                        code="E_SESSION_NOT_FOUND",
                        message="active session not found",
                        details={"session_id": session_id},
                        retryable=False,
                    )

                prior_turns = db.scalars(
                    select(TurnRecord)
                    .where(TurnRecord.session_id == session_id)
                    .order_by(TurnRecord.created_at.asc(), TurnRecord.id.asc())
                ).all()
                runtime_provenance = _runtime_provenance_for_turn(
                    db=db,
                    prior_turns=prior_turns,
                    max_recent_turns=int(app.state.max_recent_turns),
                )
                context_bundle = _build_turn_context_bundle(
                    prior_turns=prior_turns,
                    max_recent_turns=app.state.max_recent_turns,
                )
                context_metadata = _context_bundle_audit_metadata(context_bundle)

                now = _utcnow()
                turn = TurnRecord(
                    id=_new_id("trn"),
                    session_id=session_id,
                    user_message=payload.message,
                    assistant_message=None,
                    status="in_progress",
                    created_at=now,
                    updated_at=now,
                )
                db.add(turn)
                db.flush()

                sequence = 0
                created_events: list[EventRecord] = []
                created_action_attempts: list[ActionAttemptRecord] = []

                def add_event(event_type: str, payload_data: dict[str, Any]) -> None:
                    nonlocal sequence
                    sequence += 1
                    event = EventRecord(
                        id=_new_id("evn"),
                        session_id=session_id,
                        turn_id=turn.id,
                        sequence=sequence,
                        event_type=event_type,
                        payload=jsonable_encoder(payload_data),
                        created_at=_utcnow(),
                    )
                    db.add(event)
                    created_events.append(event)

                add_event("evt.turn.started", {"message": payload.message})
                applied_limits = _applied_turn_limits(app)

                def elapsed_turn_ms(started_at: float) -> int:
                    return int((time.perf_counter() - started_at) * 1000)

                def build_turn_limit_failure(
                    *,
                    budget: str,
                    unit: str,
                    measured: int,
                    limit: int,
                ) -> ApiError:
                    return _build_turn_limit_error(
                        session_id=session_id,
                        turn_id=turn.id,
                        violation=TurnLimitViolation(
                            budget=budget,
                            unit=unit,
                            measured=measured,
                            limit=limit,
                        ),
                        applied_limits=applied_limits,
                    )

                def emit_turn_limit_failure(failure: ApiError) -> None:
                    raw_limit = failure.details.get("limit")
                    limit_details = raw_limit if isinstance(raw_limit, dict) else {}
                    add_event(
                        "evt.turn.limit_reached",
                        {
                            "code": failure.code,
                            "message": failure.message,
                            "limit": limit_details,
                            "applied_limits": applied_limits,
                        },
                    )
                    add_event(
                        "evt.assistant.emitted",
                        {
                            "message": failure.message,
                            "bounded_failure": {
                                "code": failure.code,
                                "limit": limit_details,
                            },
                        },
                    )
                    turn.assistant_message = failure.message
                    turn.status = "failed"
                    turn.updated_at = _utcnow()
                    add_event(
                        "evt.turn.failed",
                        {
                            "failure_reason": failure.message,
                            "error_code": failure.code,
                            "limit": limit_details,
                        },
                    )

                bounded_failure: ApiError | None = None
                model_failure: ApiError | None = None
                model_failure_reason: str | None = None
                assistant_response: dict[str, Any] | None = None

                context_tokens = _estimate_context_tokens(
                    context_bundle=context_bundle,
                    user_message=payload.message,
                )
                if context_tokens > app.state.max_context_tokens:
                    bounded_failure = build_turn_limit_failure(
                        budget="context_tokens",
                        unit="tokens",
                        measured=context_tokens,
                        limit=app.state.max_context_tokens,
                    )
                else:
                    turn_started_at = time.perf_counter()
                    for attempt in range(1, app.state.max_model_attempts + 1):
                        if attempt > 1:
                            elapsed_before_attempt_ms = elapsed_turn_ms(turn_started_at)
                            if elapsed_before_attempt_ms > app.state.max_turn_wall_time_ms:
                                bounded_failure = build_turn_limit_failure(
                                    budget="turn_wall_time_ms",
                                    unit="ms",
                                    measured=elapsed_before_attempt_ms,
                                    limit=app.state.max_turn_wall_time_ms,
                                )
                                break

                        add_event(
                            "evt.model.started",
                            {
                                "provider": app.state.model_adapter.provider,
                                "model": app.state.model_adapter.model,
                                "context": context_metadata,
                                "attempt": attempt,
                            },
                        )
                        model_started_at = time.perf_counter()
                        try:
                            candidate_response = app.state.model_adapter.respond(
                                payload.message,
                                session_id=session_id,
                                turn_id=turn.id,
                                history=context_bundle["recent_active_session_turns"],
                                context_bundle=context_bundle,
                            )
                            duration_ms = int((time.perf_counter() - model_started_at) * 1000)
                            add_event(
                                "evt.model.completed",
                                {
                                    "provider": candidate_response["provider"],
                                    "model": candidate_response["model"],
                                    "duration_ms": duration_ms,
                                    "usage": candidate_response.get("usage"),
                                    "provider_response_id": candidate_response.get(
                                        "provider_response_id"
                                    ),
                                    "attempt": attempt,
                                },
                            )

                            elapsed_after_model_ms = elapsed_turn_ms(turn_started_at)
                            if elapsed_after_model_ms > app.state.max_turn_wall_time_ms:
                                bounded_failure = build_turn_limit_failure(
                                    budget="turn_wall_time_ms",
                                    unit="ms",
                                    measured=elapsed_after_model_ms,
                                    limit=app.state.max_turn_wall_time_ms,
                                )
                                break

                            assistant_text = candidate_response.get("assistant_text")
                            if not isinstance(assistant_text, str) or not assistant_text.strip():
                                raise RuntimeError("model response missing assistant_text")
                            response_tokens = _response_tokens_from_model_payload(
                                candidate_response,
                                assistant_text=assistant_text,
                            )
                            if response_tokens > app.state.max_response_tokens:
                                bounded_failure = build_turn_limit_failure(
                                    budget="response_tokens",
                                    unit="tokens",
                                    measured=response_tokens,
                                    limit=app.state.max_response_tokens,
                                )
                                break

                            assistant_response = candidate_response
                            break
                        except Exception as exc:
                            duration_ms = int((time.perf_counter() - model_started_at) * 1000)
                            fallback_reason = f"unexpected {exc.__class__.__name__}"
                            should_retry = False
                            if isinstance(exc, ModelAdapterError):
                                failure_reason = safe_failure_reason(
                                    exc.safe_reason,
                                    fallback=fallback_reason,
                                )
                                model_failure_candidate = ApiError(
                                    status_code=exc.status_code,
                                    code=exc.code,
                                    message=exc.message,
                                    details={
                                        "session_id": session_id,
                                        "turn_id": turn.id,
                                        "attempt": attempt,
                                    },
                                    retryable=exc.retryable,
                                )
                                should_retry = exc.retryable
                            else:
                                failure_reason = safe_failure_reason(
                                    str(exc),
                                    fallback=fallback_reason,
                                )
                                model_failure_candidate = ApiError(
                                    status_code=502,
                                    code="E_MODEL_FAILURE",
                                    message="model provider request failed",
                                    details={
                                        "session_id": session_id,
                                        "turn_id": turn.id,
                                        "attempt": attempt,
                                    },
                                    retryable=True,
                                )

                            add_event(
                                "evt.model.failed",
                                {
                                    "provider": app.state.model_adapter.provider,
                                    "model": app.state.model_adapter.model,
                                    "duration_ms": duration_ms,
                                    "failure_reason": failure_reason,
                                    "attempt": attempt,
                                },
                            )

                            elapsed_after_failure_ms = elapsed_turn_ms(turn_started_at)
                            if elapsed_after_failure_ms > app.state.max_turn_wall_time_ms:
                                bounded_failure = build_turn_limit_failure(
                                    budget="turn_wall_time_ms",
                                    unit="ms",
                                    measured=elapsed_after_failure_ms,
                                    limit=app.state.max_turn_wall_time_ms,
                                )
                                break
                            if should_retry and attempt < app.state.max_model_attempts:
                                continue
                            if should_retry and attempt >= app.state.max_model_attempts:
                                bounded_failure = build_turn_limit_failure(
                                    budget="model_attempts",
                                    unit="attempts",
                                    measured=attempt,
                                    limit=app.state.max_model_attempts,
                                )
                                break

                            model_failure = model_failure_candidate
                            model_failure_reason = failure_reason
                            break

                if bounded_failure is not None:
                    emit_turn_limit_failure(bounded_failure)
                elif model_failure is not None:
                    turn.status = "failed"
                    turn.updated_at = _utcnow()
                    add_event(
                        "evt.turn.failed",
                        {"failure_reason": model_failure_reason or "model provider request failed"},
                    )
                else:
                    assert assistant_response is not None
                    proposal_processing = process_action_proposals(
                        db=db,
                        session_id=session_id,
                        turn=turn,
                        assistant_message=assistant_response["assistant_text"],
                        proposals_raw=assistant_response.get("action_proposals"),
                        approval_ttl_seconds=int(app.state.approval_ttl_seconds),
                        approval_actor_id=str(app.state.approval_actor_id),
                        add_event=add_event,
                        now_fn=_utcnow,
                        new_id_fn=_new_id,
                        runtime_provenance=runtime_provenance,
                    )
                    created_action_attempts.extend(proposal_processing.action_attempts)

                    turn.assistant_message = proposal_processing.assistant_message
                    turn.status = "completed"
                    turn.updated_at = _utcnow()
                    add_event(
                        "evt.assistant.emitted",
                        {"message": proposal_processing.assistant_message},
                    )
                    add_event("evt.turn.completed", {})

                active_session.updated_at = _utcnow()
                db.flush()

                if bounded_failure is not None:
                    return _error_response(bounded_failure)
                if model_failure is not None:
                    return _error_response(model_failure)

                assert assistant_response is not None
                approvals_by_attempt_id = {
                    approval.action_attempt_id: approval
                    for approval in db.scalars(
                        select(ApprovalRequestRecord).where(
                            ApprovalRequestRecord.action_attempt_id.in_(
                                [attempt.id for attempt in created_action_attempts]
                            )
                        )
                    ).all()
                } if created_action_attempts else {}
                serialized_action_attempts = [
                    serialize_action_attempt(
                        action_attempt,
                        approval=approvals_by_attempt_id.get(action_attempt.id),
                    )
                    for action_attempt in created_action_attempts
                ]
                return {
                    "ok": True,
                    "session": serialize_session(active_session),
                    "turn": serialize_turn(
                        turn,
                        events=created_events,
                        action_attempts=serialized_action_attempts,
                    ),
                    "assistant": {
                        "message": turn.assistant_message,
                        "provider": assistant_response["provider"],
                        "model": assistant_response["model"],
                    },
                }

    @app.post("/v1/approvals", response_model=None)
    def post_approval_decision(
        payload: ApprovalDecisionRequest,
    ) -> JSONResponse | dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            with db.begin():
                try:
                    actor_id = payload.actor_id or str(app.state.approval_actor_id)
                    decision_result = resolve_approval_decision(
                        db=db,
                        approval_ref=payload.approval_ref,
                        decision=payload.decision,
                        actor_id=actor_id,
                        reason=payload.reason,
                        now_fn=_utcnow,
                        new_id_fn=_new_id,
                    )
                except ActionRuntimeError as exc:
                    return _error_response(
                        ApiError(
                            status_code=exc.status_code,
                            code=exc.code,
                            message=exc.message,
                            details=exc.details,
                            retryable=exc.retryable,
                        )
                    )

                return {
                    "ok": True,
                    "approval": {
                        "reference": decision_result.approval.id,
                        "status": decision_result.approval.status,
                        "reason": (
                            redact_text(decision_result.approval.decision_reason)
                            if isinstance(decision_result.approval.decision_reason, str)
                            else None
                        ),
                        "expires_at": to_rfc3339(decision_result.approval.expires_at),
                        "decided_at": (
                            to_rfc3339(decision_result.approval.decided_at)
                            if decision_result.approval.decided_at is not None
                            else None
                        ),
                    },
                    "assistant": {"message": decision_result.assistant_message},
                }

    @app.get("/v1/sessions/{session_id}/events")
    def get_session_events(session_id: str) -> dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            session_record = db.scalar(
                select(SessionRecord).where(SessionRecord.id == session_id).limit(1)
            )
            if session_record is None:
                raise ApiError(
                    status_code=404,
                    code="E_SESSION_NOT_FOUND",
                    message="session not found",
                    details={"session_id": session_id},
                    retryable=False,
                )

            turns = db.scalars(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(TurnRecord.created_at.asc(), TurnRecord.id.asc())
            ).all()
            turn_ids = [turn.id for turn in turns]

            events_by_turn: dict[str, list[EventRecord]] = {turn_id: [] for turn_id in turn_ids}
            action_attempts_by_turn: dict[str, list[ActionAttemptRecord]] = {
                turn_id: [] for turn_id in turn_ids
            }
            approvals_by_attempt_id: dict[str, ApprovalRequestRecord] = {}
            if turn_ids:
                for event in db.scalars(
                    select(EventRecord)
                    .where(EventRecord.turn_id.in_(turn_ids))
                    .order_by(
                        EventRecord.created_at.asc(),
                        EventRecord.id.asc(),
                    )
                ).all():
                    events_by_turn[event.turn_id].append(event)
                for turn_events in events_by_turn.values():
                    turn_events.sort(key=lambda event: (event.sequence, event.created_at, event.id))

                action_attempts = db.scalars(
                    select(ActionAttemptRecord)
                    .where(ActionAttemptRecord.turn_id.in_(turn_ids))
                    .order_by(
                        ActionAttemptRecord.proposal_index.asc(),
                        ActionAttemptRecord.created_at.asc(),
                        ActionAttemptRecord.id.asc(),
                    )
                ).all()
                for action_attempt in action_attempts:
                    action_attempts_by_turn[action_attempt.turn_id].append(action_attempt)

                action_attempt_ids = [action_attempt.id for action_attempt in action_attempts]
                if action_attempt_ids:
                    approvals = db.scalars(
                        select(ApprovalRequestRecord).where(
                            ApprovalRequestRecord.action_attempt_id.in_(action_attempt_ids)
                        )
                    ).all()
                    approvals_by_attempt_id = {
                        approval.action_attempt_id: approval for approval in approvals
                    }

            serialized_turns = [
                serialize_turn(
                    turn,
                    events=events_by_turn.get(turn.id, []),
                    action_attempts=[
                        serialize_action_attempt(
                            action_attempt,
                            approval=approvals_by_attempt_id.get(action_attempt.id),
                        )
                        for action_attempt in action_attempts_by_turn.get(turn.id, [])
                    ],
                )
                for turn in turns
            ]
            return {"ok": True, "session_id": session_id, "turns": serialized_turns}

    return app
