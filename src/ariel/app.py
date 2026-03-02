from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Protocol

import httpx
import ulid
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from ariel.config import AppSettings
from ariel.db import missing_required_tables, reset_schema_for_tests


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_rfc3339(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    turns: Mapped[list["TurnRecord"]] = relationship(back_populates="session")

    __table_args__ = (
        Index(
            "ix_single_active_session",
            "is_active",
            unique=True,
            postgresql_where=(is_active.is_(True)),
        ),
    )


class TurnRecord(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[SessionRecord] = relationship(back_populates="turns")
    events: Mapped[list["EventRecord"]] = relationship(back_populates="turn")

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_turn_status",
        ),
    )


class EventRecord(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    turn: Mapped[TurnRecord] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_event_sequence_positive"),
        Index("ix_turn_sequence_unique", "turn_id", "sequence", unique=True),
    )


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)

    @field_validator("message")
    @classmethod
    def _message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


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


_SECRET_LIKE_PATTERN = re.compile(
    (
        r"(sk-[A-Za-z0-9_\-]{8,}"
        r"|api[_-]?key"
        r"|secret(?:[_-]?(?:key|value))?"
        r"|authorization"
        r"|bearer\s+[A-Za-z0-9\-_.]+"
        r"|token\s*[:=]\s*[A-Za-z0-9\-_.]+)"
    ),
    re.IGNORECASE,
)


def _safe_failure_reason(raw_message: str, *, fallback: str) -> str:
    candidate = raw_message.strip()
    if not candidate:
        return fallback
    if _SECRET_LIKE_PATTERN.search(candidate):
        return fallback
    return candidate[:500]


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


def _serialize_session(session: SessionRecord) -> dict[str, Any]:
    return {
        "id": session.id,
        "is_active": session.is_active,
        "created_at": _to_rfc3339(session.created_at),
        "updated_at": _to_rfc3339(session.updated_at),
    }


def _serialize_turn(turn: TurnRecord, *, events: list[EventRecord]) -> dict[str, Any]:
    return {
        "id": turn.id,
        "session_id": turn.session_id,
        "user_message": turn.user_message,
        "assistant_message": turn.assistant_message,
        "status": turn.status,
        "created_at": _to_rfc3339(turn.created_at),
        "updated_at": _to_rfc3339(turn.updated_at),
        "events": [_serialize_event(event) for event in events],
    }


def _serialize_event(event: EventRecord) -> dict[str, Any]:
    return {
        "id": event.id,
        "turn_id": event.turn_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": _to_rfc3339(event.created_at),
    }


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
        return _PHONE_SURFACE_HTML

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
            return {"ok": True, "session": _serialize_session(active_session)}

    @app.get("/v1/sessions/active")
    def get_active_session() -> dict[str, Any]:
        _ensure_schema_ready()
        with session_factory() as db:
            with db.begin():
                active_session = _get_or_create_active_session(db)
            return {"ok": True, "session": _serialize_session(active_session)}

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
                                failure_reason = _safe_failure_reason(
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
                                failure_reason = _safe_failure_reason(str(exc), fallback=fallback_reason)
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
                    turn.assistant_message = assistant_response["assistant_text"]
                    turn.status = "completed"
                    turn.updated_at = _utcnow()
                    add_event(
                        "evt.assistant.emitted",
                        {"message": assistant_response["assistant_text"]},
                    )
                    add_event("evt.turn.completed", {})

                active_session.updated_at = _utcnow()
                db.flush()

                if bounded_failure is not None:
                    return _error_response(bounded_failure)
                if model_failure is not None:
                    return _error_response(model_failure)

                assert assistant_response is not None
                return {
                    "ok": True,
                    "session": _serialize_session(active_session),
                    "turn": _serialize_turn(turn, events=created_events),
                    "assistant": {
                        "message": assistant_response["assistant_text"],
                        "provider": assistant_response["provider"],
                        "model": assistant_response["model"],
                    },
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

            serialized_turns = [
                _serialize_turn(turn, events=events_by_turn.get(turn.id, [])) for turn in turns
            ]
            return {"ok": True, "session_id": session_id, "turns": serialized_turns}

    return app


_PHONE_SURFACE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ariel Chat</title>
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1115;
      color: #e6edf3;
    }
    main { max-width: 760px; margin: 0 auto; padding: 16px; }
    h1 { font-size: 1.1rem; margin: 0 0 12px; }
    #timeline {
      border: 1px solid #30363d;
      border-radius: 10px;
      padding: 12px;
      min-height: 140px;
      margin-bottom: 12px;
      background: #161b22;
    }
    .turn {
      margin-bottom: 10px;
      border-bottom: 1px solid #30363d;
      padding-bottom: 8px;
    }
    .turn:last-child { border-bottom: none; margin-bottom: 0; }
    .meta { color: #8b949e; font-size: 0.8rem; margin-bottom: 4px; }
    .event { font-size: 0.85rem; margin-left: 8px; color: #c9d1d9; }
    form { display: flex; gap: 8px; }
    input {
      flex: 1;
      font-size: 16px;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 10px;
      background: #0d1117;
      color: #e6edf3;
    }
    button {
      border: none;
      border-radius: 8px;
      padding: 10px 14px;
      background: #2f81f7;
      color: #fff;
      font-weight: 600;
      font-size: 0.95rem;
    }
    #status { margin: 8px 0; min-height: 18px; color: #8b949e; font-size: 0.85rem; }
  </style>
</head>
<body>
  <main>
    <h1>ariel chat (slice 0)</h1>
    <section id="timeline"></section>
    <div id="status"></div>
    <form id="chat-form">
      <input id="message" name="message" autocomplete="off" placeholder="type a message" required />
      <button type="submit">send</button>
    </form>
  </main>
  <script>
    let sessionId = null;

    const timelineNode = document.getElementById("timeline");
    const statusNode = document.getElementById("status");
    const formNode = document.getElementById("chat-form");
    const messageNode = document.getElementById("message");

    function setStatus(text) {
      statusNode.textContent = text;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function formatUsage(usage) {
      if (!usage || typeof usage !== "object") return "";
      const fields = [
        ["prompt", usage.prompt_tokens],
        ["completion", usage.completion_tokens],
        ["total", usage.total_tokens],
      ];
      const tokenParts = fields
        .filter(([, value]) => Number.isFinite(Number(value)))
        .map(([label, value]) => `${label}=${value}`);
      return tokenParts.length ? `tokens(${tokenParts.join(", ")})` : "";
    }

    function formatEventDetails(event) {
      const payload = (event && typeof event.payload === "object" && event.payload !== null)
        ? event.payload
        : {};
      const parts = [];
      if (payload.provider) parts.push(`provider=${payload.provider}`);
      if (payload.model) parts.push(`model=${payload.model}`);
      if (typeof payload.duration_ms === "number") parts.push(`duration_ms=${payload.duration_ms}`);
      const usage = formatUsage(payload.usage);
      if (usage) parts.push(usage);
      if (payload.failure_reason) parts.push(`failure_reason=${payload.failure_reason}`);
      return parts.join(" | ");
    }

    function renderTimeline(turns) {
      if (!turns.length) {
        timelineNode.innerHTML = "<p>no turns yet.</p>";
        return;
      }
      timelineNode.innerHTML = turns.map((turn) => {
        const events = turn.events
          .map((event) => {
            const detailText = formatEventDetails(event);
            const suffix = detailText ? ` - ${escapeHtml(detailText)}` : "";
            return `<div class="event">[${escapeHtml(event.sequence)}] ${escapeHtml(event.event_type)}${suffix}</div>`;
          })
          .join("");
        return `
          <article class="turn">
            <div class="meta">${escapeHtml(turn.id)} · ${escapeHtml(turn.status)}</div>
            <div><strong>user:</strong> ${escapeHtml(turn.user_message)}</div>
            <div><strong>assistant:</strong> ${escapeHtml(turn.assistant_message || "(none)")}</div>
            ${events}
          </article>
        `;
      }).join("");
    }

    async function loadTimeline() {
      const response = await fetch(`/v1/sessions/${sessionId}/events`);
      const data = await response.json();
      if (!response.ok || !data.ok) {
        setStatus(data?.error?.message || "timeline load failed");
        return;
      }
      renderTimeline(data.turns);
    }

    async function ensureSession() {
      const response = await fetch("/v1/sessions/active");
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error("session bootstrap failed");
      }
      sessionId = data.session.id;
    }

    async function sendMessage(text) {
      const response = await fetch(`/v1/sessions/${sessionId}/message`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data?.error?.message || "send failed");
      }
    }

    formNode.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = messageNode.value.trim();
      if (!text) return;
      messageNode.value = "";
      setStatus("sending...");
      try {
        await sendMessage(text);
        await loadTimeline();
        setStatus("ok");
      } catch (error) {
        setStatus(error.message);
      }
    });

    (async () => {
      try {
        await ensureSession();
        await loadTimeline();
        setStatus("ready");
      } catch (error) {
        setStatus(error.message);
      }
    })();
  </script>
</body>
</html>
"""
