from __future__ import annotations

from contextlib import asynccontextmanager
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Protocol

import ulid
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
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

from ariel.db import missing_required_tables, reset_schema_for_tests


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_rfc3339(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ulid.new().str.lower()}"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARIEL_", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/ariel"


_ACTIVE_SESSION_LOCK_ID = 24_310_001


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
    ) -> dict[str, Any]:
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
    db_url = database_url or AppSettings().database_url
    adapter = model_adapter or EchoModelAdapter()

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
                history = [
                    {
                        "user_message": turn.user_message,
                        "assistant_message": turn.assistant_message,
                        "status": turn.status,
                    }
                    for turn in prior_turns
                ]

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
                        payload=payload_data,
                        created_at=_utcnow(),
                    )
                    db.add(event)
                    created_events.append(event)

                add_event("evt.turn.started", {"message": payload.message})
                add_event(
                    "evt.model.started",
                    {
                        "provider": app.state.model_adapter.provider,
                        "model": app.state.model_adapter.model,
                    },
                )

                started_at = time.perf_counter()
                model_failure: ApiError | None = None
                assistant_response: dict[str, Any] | None = None
                try:
                    assistant_response = app.state.model_adapter.respond(
                        payload.message,
                        session_id=session_id,
                        turn_id=turn.id,
                        history=history,
                    )
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    add_event(
                        "evt.model.completed",
                        {
                            "provider": assistant_response["provider"],
                            "model": assistant_response["model"],
                            "duration_ms": duration_ms,
                            "usage": assistant_response.get("usage"),
                            "provider_response_id": assistant_response.get("provider_response_id"),
                        },
                    )
                    turn.assistant_message = assistant_response["assistant_text"]
                    turn.status = "completed"
                    turn.updated_at = _utcnow()
                    add_event(
                        "evt.assistant.emitted",
                        {"message": assistant_response["assistant_text"]},
                    )
                    add_event("evt.turn.completed", {})
                except Exception as exc:
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    failure_reason = str(exc) or exc.__class__.__name__
                    add_event(
                        "evt.model.failed",
                        {
                            "provider": app.state.model_adapter.provider,
                            "model": app.state.model_adapter.model,
                            "duration_ms": duration_ms,
                            "failure_reason": failure_reason,
                        },
                    )
                    turn.status = "failed"
                    turn.updated_at = _utcnow()
                    add_event("evt.turn.failed", {"failure_reason": failure_reason})
                    model_failure = ApiError(
                        status_code=502,
                        code="E_MODEL_FAILURE",
                        message="model provider request failed",
                        details={"session_id": session_id, "turn_id": turn.id},
                        retryable=True,
                    )

                active_session.updated_at = _utcnow()
                db.flush()

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
