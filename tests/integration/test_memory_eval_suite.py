"""WS-10 long-memory eval suite and single-signal regression gate.

``test_long_memory_eval_suite_passes`` seeds the canonical memory the
``LONG_MEMORY_EVAL_CASES`` fixture describes, runs ``run_memory_eval`` against
the real Reciprocal Rank Fusion pipeline, and asserts every memory.md eval case
passes and the full metric set lands on ``MemoryEvalRunRecord``.

``test_eval_suite_fails_under_single_signal_retrieval`` seeds the adversarial
``ADVERSARIAL_EVAL_CASES`` corpus, drives ``run_memory_eval`` with the vector
and lexical signal functions monkeypatched to fixed rankings, and proves the
suite passes hybrid but fails under vector-only and keyword-only retrieval.

This file imitates the integration setup of ``test_north_star_memory_pass.py``:
the same client builder, the same ``_fake_memory_*`` retrieval fixtures, and the
same candidate-seeding helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
import json
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

import ariel.memory as memory
from ariel.app import ModelAdapter, create_app
from ariel.config import AppSettings
from ariel.memory import (
    process_memory_graph_projection_job,
    process_memory_projection_job,
    run_memory_eval,
)
from ariel.persistence import (
    MemoryAssertionRecord,
    MemoryContextBlockRecord,
    MemoryEvalRunRecord,
)
from ariel.proactivity import process_proactive_deliberation_due, upsert_proactive_observation
from tests.fixtures.memory_eval_cases import ADVERSARIAL_EVAL_CASES, LONG_MEMORY_EVAL_CASES
from tests.integration.responses_helpers import responses_run_message

_id_counter = count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_mes_{next(_id_counter)}"


def _settings(**overrides: Any) -> AppSettings:
    return cast(AppSettings, cast(Any, AppSettings)(_env_file=None, **overrides))


def _session_factory(client: TestClient) -> Any:
    return cast(Any, client.app).state.session_factory


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    return TestClient(
        create_app(database_url=postgres_url, model_adapter=adapter, reset_database=True)
    )


@dataclass
class _ProbeAdapter:
    """The client model adapter. Memory recall fixtures are monkeypatched in, so
    the adapter only needs to answer chat turns the suite does not exercise."""

    provider: str = "provider.memory-eval"
    model: str = "model.memory-eval"

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, history, context_bundle
        return responses_run_message(
            assistant_text=f"assistant::{user_message}",
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_memory_eval",
            input_tokens=8,
            output_tokens=4,
        )


@dataclass
class _RememberAdapter:
    """Proactive-deliberation adapter that always decides to remember a fixed
    project-state fact, so the proactive-feedback case has a memory learned via
    the proactive candidate -> review -> active path."""

    subject_key: str
    predicate: str
    value: str

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del input_items, tools, user_message, history, context_bundle
        return {
            "provider": "provider.memory-eval",
            "model": "model.memory-eval",
            "provider_response_id": "resp_memory_eval_remember",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "decision": "remember",
                                    "confidence": 0.9,
                                    "urgency": "normal",
                                    "rationale": "The observation is durable project state.",
                                    "evidence_refs": ["latest_observation"],
                                    "tool_refs": [],
                                    "actions": [],
                                    "follow_up": None,
                                    "memory": {
                                        "subject_key": self.subject_key,
                                        "predicate": self.predicate,
                                        "value": self.value,
                                        "assertion_type": "project_state",
                                    },
                                }
                            ),
                        }
                    ],
                }
            ],
        }


# Each topic word maps to its own embedding dimension: text sharing a topic word
# is close in cosine space, unrelated text is orthogonal. Identical to the
# fixture in test_north_star_memory_pass so the suite exercises a genuine,
# non-degenerate vector signal.
_EMBEDDING_TOPIC_DIMENSIONS: dict[str, int] = {
    "phoenix": 0,
    "notebook": 1,
    "zebra": 2,
    "migration": 3,
    "deploy": 4,
    "smoke": 5,
    "worker": 6,
    "espresso": 7,
    "milestone": 8,
    "vendor": 9,
}


def _fake_memory_embedding(text: str, *, settings: AppSettings) -> list[float]:
    vector = [0.0] * settings.memory_embedding_dimensions
    lowered = text.lower()
    for token, dimension in _EMBEDDING_TOPIC_DIMENSIONS.items():
        if token in lowered:
            vector[dimension] = 1.0
    if not any(vector):
        vector[settings.memory_embedding_dimensions - 1] = 1.0
    norm = sum(component * component for component in vector) ** 0.5
    return [component / norm for component in vector]


def _rrf_curation(
    *,
    user_message: str,
    history: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    max_selected: int,
    settings: AppSettings,
) -> dict[str, Any]:
    """Curation fixture that selects the highest fused-rrf candidates and omits
    the rest. It is relevance-aware only in that it trusts the deterministic RRF
    pool ordering, which is exactly what an adversarial retrieval test needs."""
    del user_message, history, settings
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate["retrieval_features"]["rrf_score"]),
            str(candidate["id"]),
        ),
    )
    selected = [
        {"id": item["id"], "kind": item.get("kind", "semantic_assertion"), "rationale": "top rrf"}
        for item in ranked[:max_selected]
    ]
    selected_ids = {item["id"] for item in selected}
    return {
        "selected_memories": selected,
        "omitted_memories": [
            {
                "id": item["id"],
                "kind": item.get("kind", "semantic_assertion"),
                "rationale": "lower fused rrf score",
            }
            for item in candidates
            if item["id"] not in selected_ids
        ],
        "rationale": "fixture rrf curation",
        "uncertainty": "",
        "confidence": 0.9,
        "model": "fixture-memory-curator",
        "prompt_version": memory.MEMORY_CURATION_PROMPT_VERSION,
        "provider_response_id": "resp_fixture_memory_curator",
        "parse_status": "parsed",
    }


def _use_fake_memory_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "embed_memory_text", _fake_memory_embedding)
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _rrf_curation)


def _session_id(client: TestClient) -> str:
    response = client.get("/v1/sessions/active")
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _candidate(
    client: TestClient,
    *,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    evidence_text: str,
    scope_key: str = "global",
) -> str:
    response = client.post(
        "/v1/memory/candidates",
        json={
            "subject_key": subject_key,
            "predicate": predicate,
            "assertion_type": assertion_type,
            "value": value,
            "evidence_text": evidence_text,
            "confidence": 0.94,
            "scope_key": scope_key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["candidates"][0]["id"]


def _approve(client: TestClient, assertion_id: str) -> None:
    assert client.post(f"/v1/memory/candidates/{assertion_id}/approve").status_code == 200


def _drain_embedding_jobs(client: TestClient) -> None:
    """Process every pending embedding projection job so the vector signal sees
    each activated assertion."""
    while process_memory_projection_job(
        session_factory=_session_factory(client),
        settings=_settings(),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    ):
        pass


def _seed_long_memory_corpus(client: TestClient) -> dict[str, str]:
    """Seed the canonical memory the LONG_MEMORY_EVAL_CASES fixture references.
    Returns the case-label -> assertion-id map the suite resolves the fixture
    against."""
    now = datetime(2026, 5, 15, 9, 0, tzinfo=UTC)
    ids: dict[str, str] = {}

    # Temporal validity: two milestone deadlines; only one is still valid now.
    stale = _candidate(
        client,
        subject_key="project:milestone-alpha",
        predicate="project.open_question",
        assertion_type="project_state",
        value="milestone alpha deadline already passed last quarter",
        evidence_text="The user noted the milestone alpha deadline.",
    )
    _approve(client, stale)
    ids["temporal_stale"] = stale
    response = client.post(
        "/v1/memory/candidates",
        json={
            "subject_key": "project:milestone-beta",
            "predicate": "project.open_question",
            "assertion_type": "project_state",
            "value": "milestone beta deadline is the next checkpoint we track",
            "evidence_text": "The user said the milestone beta deadline is upcoming.",
            "confidence": 0.94,
            "valid_from": "2026-05-01T00:00:00Z",
            "valid_to": "2027-05-01T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    valid = response.json()["candidates"][0]["id"]
    _approve(client, valid)
    ids["temporal_valid"] = valid

    # Conflict: two contradicting single-valued phoenix deadlines open a conflict.
    first = _candidate(
        client,
        subject_key="project:phoenix",
        predicate="project.deadline",
        assertion_type="project_state",
        value="phoenix ships this friday",
        evidence_text="The user said phoenix ships this friday.",
    )
    _approve(client, first)
    second = _candidate(
        client,
        subject_key="project:phoenix",
        predicate="project.deadline",
        assertion_type="project_state",
        value="phoenix ships next month",
        evidence_text="The user said phoenix ships next month.",
    )
    assert client.post(f"/v1/memory/candidates/{second}/approve").status_code == 409

    # Abstain: an in-scope memory that does not answer the query. It is scoped to
    # project:abstainzone (subject user:default, so no entity term leaks into the
    # query) and a normal binding makes the abstain query resolve to that scope.
    abstain_decoy = _candidate(
        client,
        subject_key="user:default",
        predicate="preference.code_style",
        assertion_type="preference",
        value="the migration spreadsheet header color is blue",
        evidence_text="The user mentioned a spreadsheet header color.",
        scope_key="project:abstainzone",
    )
    _approve(client, abstain_decoy)
    ids["abstain_decoy"] = abstain_decoy
    assert (
        client.put(
            "/v1/memory/scope-bindings",
            json={
                "scope_type": "project",
                "scope_key": "project:abstainzone",
                "memory_mode": "normal",
                "reason": "abstain scope is active but holds nothing relevant",
            },
        ).status_code
        == 200
    )

    # Correction / supersession: correcting an assertion supersedes the original.
    original = _candidate(
        client,
        subject_key="project:zebra",
        predicate="project.open_question",
        assertion_type="project_state",
        value="the zebra release date is a first guess of june",
        evidence_text="The user gave a first guess for the zebra release.",
    )
    _approve(client, original)
    ids["zebra_original"] = original
    correction = client.post(
        f"/v1/memory/assertions/{original}/correct",
        json={"value": "the zebra release lands in november for users"},
    )
    assert correction.status_code == 200, correction.text
    with _session_factory(client)() as db:
        corrected_id = db.scalar(
            select(MemoryAssertionRecord.id).where(
                MemoryAssertionRecord.subject_key == "project:zebra",
                MemoryAssertionRecord.predicate == "project.open_question",
                MemoryAssertionRecord.lifecycle_state == "active",
            )
        )
    assert isinstance(corrected_id, str)
    ids["zebra_corrected"] = corrected_id

    # Deletion: a deleted assertion is gone from recall, content and all.
    deleted = _candidate(
        client,
        subject_key="project:vendor",
        predicate="project.open_question",
        assertion_type="project_state",
        value="the vendor onboarding secret is code indigo",
        evidence_text="The user shared the vendor onboarding secret.",
    )
    _approve(client, deleted)
    ids["vendor_deleted"] = deleted
    assert client.delete(f"/v1/memory/assertions/{deleted}").status_code == 200

    # no_memory mode: a project bound to no_memory blocks recall. The memory is
    # approved before the binding so the approval write is itself allowed.
    blocked = _candidate(
        client,
        subject_key="project:nomemoryzone",
        predicate="project.open_question",
        assertion_type="project_state",
        value="the nomemoryzone deadline is end of week",
        evidence_text="The user mentioned the nomemoryzone deadline.",
    )
    _approve(client, blocked)
    ids["nomemory_blocked"] = blocked
    assert (
        client.put(
            "/v1/memory/scope-bindings",
            json={
                "scope_type": "project",
                "scope_key": "project:nomemoryzone",
                "memory_mode": "no_memory",
                "reason": "this project is not remembered",
            },
        ).status_code
        == 200
    )

    # Proactive feedback: a memory learned through proactive deliberation.
    case_id = _seed_proactive_case(client, now=now)
    process_proactive_deliberation_due(
        session_factory=_session_factory(client),
        task_payload={"case_id": case_id},
        settings=_settings(),
        model_adapter=_RememberAdapter(
            subject_key="project:proactivezone",
            predicate="project.deadline",
            value="the deploy review is scheduled for next sprint",
        ),
        now_fn=lambda: now,
        new_id_fn=_new_id,
    )
    with _session_factory(client)() as db:
        proactive_id = db.scalar(
            select(MemoryAssertionRecord.id).where(
                MemoryAssertionRecord.subject_key == "project:proactivezone"
            )
        )
    assert isinstance(proactive_id, str)
    _approve(client, proactive_id)
    ids["proactive_memory"] = proactive_id

    # Negative memory: a rejected approach surfaced as the negative_memory kind.
    negative = _candidate(
        client,
        subject_key="repo:negativezone",
        predicate="negative.rejected_approach",
        assertion_type="negative",
        value="do not retry the espresso cache warmup approach",
        evidence_text="The espresso cache warmup approach was rejected after it stalled.",
    )
    _approve(client, negative)
    ids["negative_rejected"] = negative

    # Graph relationship reasoning: the answer is two hops away in the entity
    # graph (migration -> smoke -> worker), reachable only via the graph signal.
    chain: list[str] = []
    for subject, topic in (
        ("project:migration", "migration"),
        ("project:smoke", "smoke"),
        ("project:worker", "worker"),
    ):
        node = _candidate(
            client,
            subject_key=subject,
            predicate="project.deadline",
            assertion_type="project_state",
            value=f"the {topic} stage ships on its own track",
            evidence_text=f"The user described the {topic} stage.",
        )
        _approve(client, node)
        chain.append(node)
    ids["graph_two_hop"] = chain[2]
    _drain_embedding_jobs(client)
    with _session_factory(client)() as db:
        entity_ids = [
            cast(MemoryAssertionRecord, db.get(MemoryAssertionRecord, node)).subject_entity_id
            for node in chain
        ]
        evidence_id = db.scalar(
            select(memory.MemoryAssertionEvidenceRecord.evidence_id)
            .where(memory.MemoryAssertionEvidenceRecord.assertion_id == chain[0])
            .limit(1)
        )
    assert isinstance(evidence_id, str)
    for source_entity_id, target_entity_id in (
        (entity_ids[0], entity_ids[1]),
        (entity_ids[1], entity_ids[2]),
    ):
        relationship = client.post(
            "/v1/memory/relationships",
            json={
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "relationship_type": "depends_on",
                "evidence_id": evidence_id,
                "scope_key": "global",
                "confidence": 0.9,
            },
        )
        assert relationship.status_code == 200, relationship.text
    while process_memory_graph_projection_job(
        session_factory=_session_factory(client),
        now_fn=lambda: datetime.now(tz=UTC),
        new_id_fn=_new_id,
    ):
        pass

    # Hot-index budget pressure: many open questions for the hotindexzone scope,
    # whose hot index is then consolidated under a tight token budget. A normal
    # scope binding makes the hot-index query resolve to this scope, isolating
    # its candidate pool.
    for index in range(12):
        entry = _candidate(
            client,
            subject_key="project:hotindexzone",
            predicate="project.open_question",
            assertion_type="project_state",
            value=f"hotindexzone open question number {index}",
            evidence_text=f"The user raised hotindexzone open question {index}.",
        )
        _approve(client, entry)
    assert (
        client.put(
            "/v1/memory/scope-bindings",
            json={
                "scope_type": "project",
                "scope_key": "project:hotindexzone",
                "memory_mode": "normal",
                "reason": "hotindexzone scope is active and queried directly",
            },
        ).status_code
        == 200
    )
    with _session_factory(client)() as db:
        with db.begin():
            memory.consolidate_memory(
                db,
                scope_key="project:hotindexzone",
                actor_id="system",
                now_fn=lambda: now,
                new_id_fn=_new_id,
                settings=_settings(memory_hot_index_budget_tokens=40),
            )
    with _session_factory(client)() as db:
        hot_block = db.scalar(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.block_type == "hot_index",
                MemoryContextBlockRecord.scope_key == "project:hotindexzone",
            )
        )
    assert hot_block is not None
    # The rebuilt hot index was genuinely held inside the tight token budget.
    assert memory.count_context_tokens(hot_block.content) <= 40
    ids["hot_index_block"] = hot_block.id

    _drain_embedding_jobs(client)
    return ids


def _seed_proactive_case(client: TestClient, *, now: datetime) -> str:
    with _session_factory(client)() as db:
        with db.begin():
            case_id = upsert_proactive_observation(
                db,
                dedupe_key=f"dedupe:{_new_id('obs')}",
                case_key=f"case:{_new_id('pca')}",
                source_type="job",
                source_id="job_memory_eval",
                observation_type="job_state",
                subject="Deploy review",
                summary="The deploy review should be scheduled.",
                payload={"status": "waiting"},
                evidence={"job_id": "job_memory_eval"},
                taint={"provenance_status": "trusted_internal"},
                trust_boundary="trusted_internal",
                observed_at=now,
                workspace_item_id=None,
                now=now,
                new_id_fn=_new_id,
            )
            assert case_id is not None
            return case_id


def _resolve_natural_cases(
    fixture_cases: list[dict[str, Any]], ids: dict[str, str]
) -> list[dict[str, Any]]:
    """Turn the label-referenced LONG_MEMORY_EVAL_CASES into the concrete case
    dicts run_memory_eval consumes, substituting seeded assertion ids."""
    return [
        {
            "query": case["query"],
            "expected_memory_ids": [ids[label] for label in case["expect_labels"]],
            "forbidden_memory_ids": [ids[label] for label in case["forbid_labels"]],
            "expected_kinds": case["expected_kinds"],
            "forbidden_texts": case["forbidden_texts"],
            "expect_conflict": case["expect_conflict"],
            "expect_policy_blocked": case["expect_policy_blocked"],
            "max_recalled_assertions": case["max_recalled_assertions"],
        }
        for case in fixture_cases
    ]


def _resolve_adversarial_cases(
    fixture_cases: list[dict[str, Any]], ids: dict[str, str]
) -> list[dict[str, Any]]:
    """Turn the ADVERSARIAL_EVAL_CASES into the concrete case dicts; the per-case
    signal rankings are resolved separately when the stubs are installed."""
    return [
        {
            "query": case["query"],
            "expected_memory_ids": [ids[case["expect_label"]]],
            "forbidden_memory_ids": [ids[case["forbid_label"]]],
            "max_recalled_assertions": case["max_recalled_assertions"],
        }
        for case in fixture_cases
    ]


def test_long_memory_eval_suite_passes(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed the canonical memory, run the full long-memory eval against the real
    # hybrid retrieval pipeline, and assert every memory.md case passes and the
    # complete metric set is recorded on MemoryEvalRunRecord.
    _use_fake_memory_models(monkeypatch)
    with _build_client(postgres_url, cast(ModelAdapter, _ProbeAdapter())) as client:
        session_id = _session_id(client)
        ids = _seed_long_memory_corpus(client)
        cases = _resolve_natural_cases(LONG_MEMORY_EVAL_CASES, ids)
        assert len(cases) == 10

        with _session_factory(client)() as db:
            with db.begin():
                result = run_memory_eval(
                    db,
                    eval_name="long-memory regression suite",
                    cases=cases,
                    now_fn=lambda: datetime.now(tz=UTC),
                    new_id_fn=_new_id,
                    settings=_settings(),
                    current_session_id=session_id,
                )

        assert result["status"] == "completed", [
            (case["query"], case["failures"])
            for case in result["cases"]
            if case["status"] == "failed"
        ]
        metrics = result["metrics"]
        assert metrics["passed_cases"] == 10
        assert metrics["failed_cases"] == 0
        # The full memory.md metric set is present and internally consistent.
        assert metrics["answer_accuracy"] == 1.0
        assert metrics["candidate_recall"] == 1.0
        assert metrics["curation_precision"] == 1.0
        assert metrics["conflict_handling_accuracy"] == 1.0
        assert metrics["selected_relevant_memory_count"] >= 1
        assert metrics["omitted_relevant_memory_count"] == 0
        assert metrics["context_tokens"] >= 0
        assert metrics["retrieval_latency_ms"] >= 0.0
        for latency_key in (
            "extraction_latency_ms",
            "retrieval_latency_ms",
            "curation_latency_ms",
            "projection_latency_ms",
            "consolidation_latency_ms",
        ):
            assert latency_key in metrics

        # The metric set is durably persisted on the eval run record.
        with _session_factory(client)() as db:
            run = db.get(MemoryEvalRunRecord, result["id"])
            assert run is not None
            assert run.status == "completed"
            for key in (
                "answer_accuracy",
                "candidate_recall",
                "curation_precision",
                "selected_relevant_memory_count",
                "omitted_relevant_memory_count",
                "conflict_handling_accuracy",
                "context_tokens",
                "extraction_latency_ms",
                "retrieval_latency_ms",
                "curation_latency_ms",
                "projection_latency_ms",
                "consolidation_latency_ms",
            ):
                assert key in run.metrics, key


def _seed_adversarial_corpus(client: TestClient) -> dict[str, str]:
    """Seed the three adversarial assertions. They are plain commitments with no
    entity, temporal, graph, or symbol signal, so retrieval over them is fully
    determined by the monkeypatched vector and lexical rankings."""
    ids: dict[str, str] = {}
    for label, value in (
        ("vector_decoy", "the vector decoy landing note"),
        ("lexical_decoy", "the lexical decoy release note"),
        ("fused_correct", "the fused correct answer note"),
    ):
        assertion_id = _candidate(
            client,
            subject_key="user:default",
            predicate="commitment.todo",
            assertion_type="commitment",
            value=value,
            evidence_text=f"The user recorded the {label} note.",
        )
        _approve(client, assertion_id)
        ids[label] = assertion_id
    return ids


def test_eval_suite_fails_under_single_signal_retrieval(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adversarial cases prove the suite is a genuine hybrid-retrieval gate:
    # each correct answer is reachable only when both the vector and lexical
    # signals run. Replaying the eval with one signal disabled must make it fail.
    monkeypatch.setattr(memory, "_curate_memory_context_with_model", _rrf_curation)
    with _build_client(postgres_url, cast(ModelAdapter, _ProbeAdapter())) as client:
        session_id = _session_id(client)
        ids = _seed_adversarial_corpus(client)
        cases = _resolve_adversarial_cases(ADVERSARIAL_EVAL_CASES, ids)
        assert len(cases) == 2

        assertions_table = "memory_assertions"

        def _vector_stub(labels: list[str]) -> Any:
            ranking = [(assertions_table, ids[label]) for label in labels]

            def signal(
                db: Any, *, user_message: str, settings: AppSettings, limit: int
            ) -> tuple[list[tuple[str, str]], dict[str, float]]:
                del db, user_message, settings, limit
                return ranking, {ref[1]: 0.1 for ref in ranking}

            return signal

        def _lexical_stub(labels: list[str]) -> Any:
            ranking = [(assertions_table, ids[label]) for label in labels]

            def signal(
                db: Any, *, user_message: str, limit: int
            ) -> tuple[list[tuple[str, str]], dict[str, int]]:
                del db, user_message, limit
                return ranking, {ref[1]: index + 1 for index, ref in enumerate(ranking)}

            return signal

        def _run(*, vector: bool, lexical: bool) -> str:
            # Each adversarial case carries its own per-signal rankings, so the
            # suite is split into one eval run per case. A disabled signal
            # contributes an empty ranking, exactly as a degenerate retrieval
            # pipeline would.
            statuses: list[str] = []
            for fixture_case, resolved_case in zip(ADVERSARIAL_EVAL_CASES, cases, strict=True):
                monkeypatch.setattr(
                    memory,
                    "_vector_signal",
                    _vector_stub(fixture_case["vector_labels"] if vector else []),
                )
                monkeypatch.setattr(
                    memory,
                    "_lexical_signal",
                    _lexical_stub(fixture_case["lexical_labels"] if lexical else []),
                )
                with _session_factory(client)() as db:
                    with db.begin():
                        result = run_memory_eval(
                            db,
                            eval_name="adversarial retrieval gate",
                            cases=[resolved_case],
                            now_fn=lambda: datetime.now(tz=UTC),
                            new_id_fn=_new_id,
                            settings=_settings(),
                            current_session_id=session_id,
                        )
                statuses.append(result["status"])
            return "completed" if all(status == "completed" for status in statuses) else "failed"

        # Hybrid: both signals run, the fused pool reaches the correct answer.
        assert _run(vector=True, lexical=True) == "completed"
        # Vector-only retrieval surfaces the vector decoy: the suite fails.
        assert _run(vector=True, lexical=False) == "failed"
        # Keyword-only retrieval surfaces the lexical decoy: the suite fails.
        assert _run(vector=False, lexical=True) == "failed"
