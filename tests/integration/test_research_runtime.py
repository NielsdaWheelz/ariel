"""Integration tests for ``run_research`` — the research subagent loop.

These drive ``run_research`` directly with a fake model adapter and the
``FakeSandboxRuntime``, in the style of ``test_run_runtime_research_finding.py``
and ``test_single_run_cutover.py``. They cover:

- the complete path (``research.finding`` ends the run →
  ``ResearchFinding(status="complete", ...)``);
- graceful non-convergence (stuck-detection and the model-call backstop end the
  run without a finding → ``ResearchFinding(status="partial", ...)``);
- model-call failure (the adapter's ``create_response`` raises →
  ``ResearchFinding(status="failed", ...)``);
- the per-run mode whitelist (a ``web`` run exposes only web read capabilities,
  a ``personal`` run only personal read capabilities — never both);
- personal-mode threading: ``google_runtime`` is passed into
  ``execute_run_program`` so personal capabilities execute against it rather
  than failing on a ``None`` runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select

from ariel.app import build_google_runtime
from ariel.config import AppSettings
from ariel.google_connector import (
    GOOGLE_CALENDAR_READ_SCOPE,
    GOOGLE_CONNECTOR_ID,
    GOOGLE_GMAIL_READ_SCOPE,
    GoogleConnectorRecord,
    GoogleWorkspaceProvider,
    _encrypt_secret,
)
from ariel.persistence import SessionRecord, TurnRecord
from ariel.research_runtime import ResearchFinding, run_research
from tests.fake_sandbox import FakeSandboxRuntime
from tests.integration.responses_helpers import empty_recall_response, is_retriever_call

NOW = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)

_FINDING_PROGRAM = (
    "agent.emit_finding(\n"
    "    summary='Investigated the question.',\n"
    "    claims=[{'statement': 'Fact A', 'sources': ['https://example.test'], "
    "'confidence': 'high'}],\n"
    "    gaps=['Could not determine X.'],\n"
    "    sources=[{'title': 'Example', 'reference': 'https://example.test', "
    "'retrieved_at': '2026-05-20T10:00:00Z'}],\n"
    ")\n"
)


def _settings(**overrides: Any) -> AppSettings:
    base = cast(AppSettings, cast(Any, AppSettings)(_env_file=None))
    if not overrides:
        return base
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None, **overrides))


def _program_response(*, source: str, provider_response_id: str) -> dict[str, Any]:
    """A model response whose single ``run`` call carries a Python program."""

    return {
        "provider": "provider.research",
        "model": "model.research-v1",
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "provider_response_id": provider_response_id,
        "output": [
            {
                "type": "function_call",
                "id": f"fc_{provider_response_id}",
                "call_id": f"call_{provider_response_id}",
                "name": "run",
                "arguments": json.dumps({"source": source}, sort_keys=True),
                "status": "completed",
            }
        ],
    }


@dataclass
class SnapshotAdapter:
    """Fake adapter: returns queued responses and snapshots each call's input."""

    provider: str = "provider.research"
    model: str = "model.research-v1"
    responses: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[list[dict[str, Any]]] = field(default_factory=list)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if is_retriever_call(input_items):
            return empty_recall_response(provider=self.provider, model=self.model)
        del tools, user_message, history, context_bundle
        self.snapshots.append(list(input_items))
        return self.responses.pop(0)


def _seed_session(session_factory: Any, session_id: str) -> None:
    """Commit one active session so ``run_research``'s commits see the FK."""
    with session_factory() as db:
        with db.begin():
            db.add(
                SessionRecord(
                    id=session_id,
                    is_active=True,
                    lifecycle_state="active",
                    rotated_from_session_id=None,
                    rotation_reason=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


def test_run_research_completed_returns_finding(session_factory: Any) -> None:
    """A run whose program calls research.finding returns a complete finding."""
    _seed_session(session_factory, "ses_research_ok")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = SnapshotAdapter(
        responses=[_program_response(source=_FINDING_PROGRAM, provider_response_id="r1")]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_ok",
            question="What is the capital of France?",
            mode="web",
        )
    sandbox.close()

    assert isinstance(finding, ResearchFinding)
    assert finding.status == "complete"
    assert finding.question == "What is the capital of France?"
    assert finding.mode == "web"
    assert finding.summary == "Investigated the question."
    assert finding.claims == [
        {"statement": "Fact A", "sources": ["https://example.test"], "confidence": "high"}
    ]
    assert finding.gaps == ["Could not determine X."]
    assert finding.sources == [
        {
            "title": "Example",
            "reference": "https://example.test",
            "retrieved_at": "2026-05-20T10:00:00Z",
        }
    ]


def test_run_research_persists_research_kind_turn(session_factory: Any) -> None:
    """A complete run persists a TurnRecord with kind='research' and the summary."""
    _seed_session(session_factory, "ses_research_turn")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = SnapshotAdapter(
        responses=[_program_response(source=_FINDING_PROGRAM, provider_response_id="rt1")]
    )
    with session_factory() as db:
        run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_turn",
            question="Question for the turn record.",
            mode="web",
        )
    sandbox.close()

    with session_factory() as db:
        turn = db.scalar(select(TurnRecord).where(TurnRecord.session_id == "ses_research_turn"))
    assert turn is not None
    assert turn.kind == "research"
    assert turn.status == "completed"
    assert turn.user_message == "Question for the turn record."
    assert turn.assistant_message == "Investigated the question."


def test_run_research_continues_then_finishes(session_factory: Any) -> None:
    """A run may emit values over rounds, then finish with research.finding."""
    _seed_session(session_factory, "ses_research_multi")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = SnapshotAdapter(
        responses=[
            _program_response(
                source="agent.emit_value(value={'subquestions': ['a', 'b']})\n",
                provider_response_id="rm1",
            ),
            _program_response(source=_FINDING_PROGRAM, provider_response_id="rm2"),
        ]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_multi",
            question="A multi-round question.",
            mode="web",
        )
    sandbox.close()

    assert finding.status == "complete"
    assert finding.summary == "Investigated the question."
    # The loop ran two model rounds: a planning emit_value round, then the
    # finding round.
    assert len(adapter.snapshots) == 2


def test_run_research_exhausts_on_stuck_detection(session_factory: Any) -> None:
    """A run that emits a byte-identical program twice halts as partial."""
    _seed_session(session_factory, "ses_research_stuck")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    repeated = "agent.emit_value(value={'round': 'same'})\n"
    adapter = SnapshotAdapter(
        responses=[
            _program_response(source=repeated, provider_response_id="rs1"),
            _program_response(source=repeated, provider_response_id="rs2"),
        ]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_stuck",
            question="A question that never converges.",
            mode="web",
        )
    sandbox.close()

    assert finding.status == "partial"
    assert finding.claims == []
    assert finding.gaps == []
    assert finding.sources == []
    assert "did not converge" in finding.summary
    # The second response triggers stuck-detection; no third call is made.
    assert len(adapter.snapshots) == 2


def test_run_research_exhausts_on_model_call_backstop(session_factory: Any) -> None:
    """The model-call backstop ends a never-finishing run as partial."""
    _seed_session(session_factory, "ses_research_backstop")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    # Each round emits a distinct value so stuck-detection never fires; the
    # backstop is the rail that ends the run. agent_loop_max_model_calls=1
    # admits model_call_count 1 and 2, then exhausts before the 3rd.
    settings = _settings(agent_loop_max_model_calls=1)
    adapter = SnapshotAdapter(
        responses=[
            _program_response(
                source=f"agent.emit_value(value={{'round': {n}}})\n",
                provider_response_id=f"rb{n}",
            )
            for n in range(1, 3)
        ]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_backstop",
            question="A question with no finding.",
            mode="web",
        )
    sandbox.close()

    assert finding.status == "partial"
    assert finding.summary != ""
    assert finding.claims == []
    # The backstop halted the loop after exactly two model calls.
    assert len(adapter.snapshots) == 2


def test_run_research_web_mode_exposes_only_web_capabilities(session_factory: Any) -> None:
    """A web run's eligible callables are the web read capabilities, no personal."""
    _seed_session(session_factory, "ses_research_web")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = SnapshotAdapter(
        responses=[_program_response(source=_FINDING_PROGRAM, provider_response_id="rw1")]
    )
    with session_factory() as db:
        run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_web",
            question="A web-mode question.",
            mode="web",
        )
    sandbox.close()

    rendered = json.dumps(adapter.snapshots[0])
    # The web whitelist's run callables are advertised to the model.
    assert "search.web" in rendered
    assert "search.news" in rendered
    assert "web.extract" in rendered
    # No personal-mode capability is advertised — the lethal-trifecta defense.
    assert "email.search" not in rendered
    assert "email.read" not in rendered
    assert "drive.search" not in rendered
    assert "calendar.list" not in rendered


def test_run_research_personal_mode_exposes_only_personal_capabilities(
    session_factory: Any,
) -> None:
    """A personal run's eligible callables are the personal read capabilities."""
    _seed_session(session_factory, "ses_research_personal")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = SnapshotAdapter(
        responses=[_program_response(source=_FINDING_PROGRAM, provider_response_id="rp1")]
    )
    with session_factory() as db:
        run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_personal",
            question="A personal-mode question.",
            mode="personal",
        )
    sandbox.close()

    rendered = json.dumps(adapter.snapshots[0])
    # The personal whitelist's run callables are advertised to the model.
    assert "email.search" in rendered
    assert "email.read" in rendered
    assert "drive.search" in rendered
    assert "drive.read" in rendered
    assert "calendar.list" in rendered
    # No web-mode capability is advertised — the lethal-trifecta defense.
    assert "search.web" not in rendered
    assert "web.extract" not in rendered


def test_run_research_web_mode_program_cannot_call_personal_capability(
    session_factory: Any,
) -> None:
    """A web run's program calling a personal capability fails: it is not eligible.

    The mode whitelist is the eligible syscall set, so ``email.search`` is not
    bound as a callable in a web run — a program that calls it fails to
    complete, and the run, finding nothing, ends as partial rather than ever
    reaching private data. This is the lethal-trifecta defense at the syscall
    surface: a web run cannot touch a personal capability even if steered to.
    """
    _seed_session(session_factory, "ses_research_xmode")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    # The program tries a personal capability in a web run, then (in a distinct
    # second program) repeats it so stuck-detection ends the run promptly.
    cross_mode = "hits = email.search(query='secret')\n"
    adapter = SnapshotAdapter(
        responses=[
            _program_response(source=cross_mode, provider_response_id="rx1"),
            _program_response(source=cross_mode, provider_response_id="rx2"),
        ]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_xmode",
            question="A question steered at private data.",
            mode="web",
        )
    sandbox.close()

    assert finding.status == "partial"
    assert finding.claims == []
    # The personal capability was unreachable: the program did not complete and
    # that failure — not a private-data read — was fed back to the model.
    feedback = json.dumps(adapter.snapshots[-1])
    assert "did not complete" in feedback


def test_run_research_model_call_failure_returns_failed_finding(
    session_factory: Any,
) -> None:
    """A model call that raises yields ResearchFinding(status='failed') and a failed TurnRecord."""

    @dataclass
    class RaisingAdapter:
        provider: str = "provider.research"
        model: str = "model.research-v1"
        snapshots: list[list[dict[str, Any]]] = field(default_factory=list)

        def create_response(
            self,
            *,
            input_items: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            user_message: str,
            history: list[dict[str, Any]],
            context_bundle: dict[str, Any],
        ) -> dict[str, Any]:
            if is_retriever_call(input_items):
                return empty_recall_response(provider=self.provider, model=self.model)
            del tools, user_message, history, context_bundle
            self.snapshots.append(list(input_items))
            raise RuntimeError("model unavailable")

    _seed_session(session_factory, "ses_research_fail")
    sandbox = FakeSandboxRuntime()
    sandbox.start()
    settings = _settings()
    adapter = RaisingAdapter()
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=build_google_runtime(settings),
            session_id="ses_research_fail",
            question="A question whose model call fails.",
            mode="web",
        )
    sandbox.close()

    assert finding.status == "failed"
    assert finding.claims == []
    assert finding.gaps == []
    assert finding.sources == []
    assert "failed" in finding.summary
    # The model was called exactly once before the exception.
    assert len(adapter.snapshots) == 1

    with session_factory() as db:
        turn = db.scalar(select(TurnRecord).where(TurnRecord.session_id == "ses_research_fail"))
    assert turn is not None
    assert turn.status == "failed"


@dataclass
class FakeCalendarProvider:
    """Minimal GoogleWorkspaceProvider fake that handles calendar.list reads."""

    calendar_list_calls: list[dict[str, Any]] = field(default_factory=list)

    def calendar_list(
        self,
        *,
        access_token: str,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        del access_token
        self.calendar_list_calls.append(dict(normalized_input))
        return {
            "schema_version": "google.calendar.events.v1",
            "events": [],
            "retrieved_at": "2026-05-19T10:00:00Z",
            "window_start": normalized_input["window_start"],
            "window_end": normalized_input["window_end"],
        }

    # Remaining protocol methods — never called in this test.
    def calendar_list_event_deltas(self, **_: Any) -> dict[str, Any]:
        return {}

    def calendar_propose_slots(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_search(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_read(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_list_history(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_get_message_label_state(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_archive(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_trash(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_modify_labels(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_undo(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_create_draft(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_send_draft(self, **_: Any) -> dict[str, Any]:
        return {}

    def email_send_new(self, **_: Any) -> dict[str, Any]:
        return {}

    def drive_search(self, **_: Any) -> dict[str, Any]:
        return {}

    def drive_read(self, **_: Any) -> dict[str, Any]:
        return {}

    def drive_share(self, **_: Any) -> dict[str, Any]:
        return {}

    def gmail_register_watch(self, **_: Any) -> dict[str, Any]:
        return {}

    def gmail_stop_watch(self, **_: Any) -> None:
        return

    def calendar_register_watch(self, **_: Any) -> dict[str, Any]:
        return {}

    def calendar_create_event(self, **_: Any) -> dict[str, Any]:
        return {}

    def calendar_update_event(self, **_: Any) -> dict[str, Any]:
        return {}

    def calendar_respond_to_event(self, **_: Any) -> dict[str, Any]:
        return {}


# A personal-mode program: read the calendar, emit the result, then finish.
_PERSONAL_FINDING_PROGRAM = (
    "result = calendar.list(window_start='2026-05-19T00:00:00Z', window_end='2026-05-20T00:00:00Z')\n"
    "agent.emit_value(value={'calendar_result': result})\n"
)

_PERSONAL_FINISH_PROGRAM = (
    "agent.emit_finding(\n"
    "    summary='Checked calendar.',\n"
    "    claims=[],\n"
    "    gaps=[],\n"
    "    sources=[],\n"
    ")\n"
)


def _seed_connected_google_connector(
    session_factory: Any,
    *,
    now: datetime,
    settings: AppSettings,
) -> None:
    """Commit a connected GoogleConnectorRecord with calendar and gmail read scopes."""
    with session_factory() as db:
        with db.begin():
            db.add(
                GoogleConnectorRecord(
                    id=GOOGLE_CONNECTOR_ID,
                    provider="google",
                    status="connected",
                    account_subject="sub_test",
                    account_email="test@example.com",
                    granted_scopes=[GOOGLE_CALENDAR_READ_SCOPE, GOOGLE_GMAIL_READ_SCOPE],
                    access_token_enc=_encrypt_secret(
                        plaintext="tok_access_test",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    refresh_token_enc=_encrypt_secret(
                        plaintext="tok_refresh_test",
                        secret=settings.connector_encryption_secret,
                        key_version=settings.connector_encryption_key_version,
                        encryption_keys=settings.connector_encryption_keys,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    token_obtained_at=now,
                    encryption_key_version=settings.connector_encryption_key_version,
                    last_error_code=None,
                    last_error_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def test_run_research_personal_mode_threads_google_runtime(
    session_factory: Any,
) -> None:
    """Personal-mode run threads google_runtime into execute_run_program.

    A personal-mode program that calls ``calendar.list`` succeeds rather than
    failing on a missing runtime: the ``FakeCalendarProvider`` receives the call
    and returns calendar data, proving that ``google_runtime`` — built with the
    fake provider — was threaded through to capability execution.
    """
    settings = _settings()
    _seed_session(session_factory, "ses_research_grt")
    _seed_connected_google_connector(session_factory, now=NOW, settings=settings)

    sandbox = FakeSandboxRuntime()
    sandbox.start()
    fake_provider = FakeCalendarProvider()
    google_runtime = build_google_runtime(
        settings, workspace_provider=cast(GoogleWorkspaceProvider, fake_provider)
    )
    adapter = SnapshotAdapter(
        responses=[
            _program_response(source=_PERSONAL_FINDING_PROGRAM, provider_response_id="grt1"),
            _program_response(source=_PERSONAL_FINISH_PROGRAM, provider_response_id="grt2"),
        ]
    )
    with session_factory() as db:
        finding = run_research(
            sandbox=sandbox,
            db=db,
            session_factory=session_factory,
            settings=settings,
            model_adapter=adapter,
            google_runtime=google_runtime,
            session_id="ses_research_grt",
            question="What is on my calendar today?",
            mode="personal",
        )
    sandbox.close()

    assert finding.status == "complete"
    assert finding.summary == "Checked calendar."
    # The fake provider received exactly one calendar.list call: google_runtime
    # was threaded through and personal-mode capability execution worked.
    assert len(fake_provider.calendar_list_calls) == 1
    assert fake_provider.calendar_list_calls[0]["window_start"] == "2026-05-19T00:00:00Z"
