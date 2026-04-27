from __future__ import annotations

import copy
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest
from testcontainers.postgres import PostgresContainer

import ariel.action_runtime as action_runtime_module
import ariel.capability_registry as capability_registry_module
import ariel.policy_engine as policy_engine_module
from ariel.app import ModelAdapter, create_app
from tests.integration.responses_helpers import responses_with_function_calls
from ariel.capability_registry import CapabilityDefinition, get_capability as registry_get_capability


@dataclass
class ActionProposalAdapter:
    provider: str = "provider.s7-pr01"
    model: str = "model.s7-pr01-v1"
    proposals_by_message: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    assistant_text_by_message: dict[str, str] = field(default_factory=dict)

    def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
        context_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        del tools, history, context_bundle
        proposals = self.proposals_by_message.get(user_message, [])
        assistant_text = self.assistant_text_by_message.get(user_message, f"assistant::{user_message}")
        return responses_with_function_calls(
            input_items=input_items,
            assistant_text=assistant_text,
            proposals=copy.deepcopy(proposals),
            provider=self.provider,
            model=self.model,
            provider_response_id="resp_s7_pr01_123",
            input_tokens=47,
            output_tokens=31,
        )


@dataclass(slots=True)
class _FakeHTTPResponse:
    status_code: int
    payload: Any = field(default_factory=dict)
    text: str = ""
    json_raises: bool = False

    def json(self) -> Any:
        if self.json_raises:
            raise ValueError("invalid json")
        return self.payload


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        url = postgres.get_connection_url()
        yield url.replace("psycopg2", "psycopg")


def _build_client(postgres_url: str, adapter: ModelAdapter) -> TestClient:
    app = create_app(
        database_url=postgres_url,
        model_adapter=adapter,
        reset_database=True,
    )
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    active = client.get("/v1/sessions/active")
    assert active.status_code == 200
    return active.json()["session"]["id"]


def _surface_attempt(turn_payload: dict[str, Any], *, proposal_index: int = 1) -> dict[str, Any]:
    lifecycle = turn_payload.get("surface_action_lifecycle")
    assert isinstance(lifecycle, list)
    assert len(lifecycle) >= proposal_index
    item = lifecycle[proposal_index - 1]
    assert isinstance(item, dict)
    return item


def _event_types(turn_payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in turn_payload["events"]]


def _assert_source_contract(source: dict[str, Any]) -> None:
    assert set(source.keys()) == {"artifact_id", "title", "source", "retrieved_at", "published_at"}
    assert isinstance(source["artifact_id"], str)
    assert source["artifact_id"].startswith("art_")
    assert isinstance(source["title"], str)
    assert isinstance(source["source"], str)
    assert isinstance(source["retrieved_at"], str)
    assert source["published_at"] is None or isinstance(source["published_at"], str)


def _patch_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capability_id: str,
    mutate: Callable[[CapabilityDefinition], CapabilityDefinition],
) -> None:
    prior_policy_lookup = policy_engine_module.get_capability
    prior_runtime_lookup = action_runtime_module.get_capability

    def patched_policy_get_capability(candidate_id: str) -> CapabilityDefinition | None:
        capability = prior_policy_lookup(candidate_id)
        if candidate_id != capability_id or capability is None:
            return capability
        return mutate(capability)

    def patched_runtime_get_capability(candidate_id: str) -> CapabilityDefinition | None:
        capability = prior_runtime_lookup(candidate_id)
        if candidate_id != capability_id or capability is None:
            return capability
        return mutate(capability)

    monkeypatch.setattr(policy_engine_module, "get_capability", patched_policy_get_capability)
    monkeypatch.setattr(action_runtime_module, "get_capability", patched_runtime_get_capability)


def _provider_payload(*, final_url: str, content: str, title: str = "Example article") -> dict[str, Any]:
    return {
        "final_url": final_url,
        "title": title,
        "retrieved_at": "2026-03-07T05:00:00Z",
        "published_at": "2026-03-06T20:00:00Z",
        "content": content,
    }


def test_s7_pr01_capability_contract_registers_web_extract_as_allowlisted_read() -> None:
    capability = registry_get_capability("cap.web.extract")
    assert capability is not None
    assert capability.impact_level == "read"
    assert capability.policy_decision == "allow_inline"
    assert capability.allowed_egress_destinations


def test_s7_pr01_web_extract_executes_inline_with_structured_output_citations_and_provenance(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT",
        "https://extract.provider.test/v1/extract",
    )
    outbound_calls: list[dict[str, Any]] = []

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, Any],
        timeout: float,
    ) -> _FakeHTTPResponse:
        outbound_calls.append(
            {
                "url": url,
                "json": copy.deepcopy(json),
                "headers": copy.deepcopy(headers),
                "timeout": timeout,
            }
        )
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://example.com/research/article/",
                content=(
                    "The article confirms the launch date and includes verifiable details "
                    "about budget and sequencing."
                ),
                title="Launch dossier",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)

    adapter = ActionProposalAdapter(
        proposals_by_message={
            "extract url": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article?utm_source=rss#section-2"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "extract url"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["proposal"]["capability_id"] == "cap.web.extract"
        assert attempt["policy"]["decision"] == "allow_inline"
        assert attempt["approval"]["status"] == "not_requested"
        assert attempt["execution"]["status"] == "succeeded"

        output = attempt["execution"]["output"]
        assert output["extract_outcome"]["status"] == "ok"
        assert output["canonical_url"] == "https://example.com/research/article"
        assert output["document"]["canonical_source"] == "https://example.com/research/article"
        assert output["document"]["truncated"] is False
        assert isinstance(output["document"]["content_blocks"], list)
        assert output["document"]["content_blocks"]

        assert len(outbound_calls) == 1
        assert outbound_calls[0]["url"] == "https://extract.provider.test/v1/extract"
        assert outbound_calls[0]["json"]["url"] == "https://example.com/research/article?utm_source=rss"

        assert "[1]" in payload["assistant"]["message"]
        assert len(payload["assistant"]["sources"]) == 1
        source = payload["assistant"]["sources"][0]
        _assert_source_contract(source)
        assert source["source"] == "https://example.com/research/article"

        artifact = client.get(f"/v1/artifacts/{source['artifact_id']}")
        assert artifact.status_code == 200
        artifact_payload = artifact.json()["artifact"]
        assert artifact_payload["id"] == source["artifact_id"]
        assert artifact_payload["source"] == source["source"]
        assert artifact_payload["retrieved_at"] == source["retrieved_at"]
        assert artifact_payload["published_at"] == source["published_at"]


@pytest.mark.parametrize(
    ("input_url", "expected_error", "expected_hint"),
    [
        ("definitely-not-a-url", "url_invalid", "valid"),
        ("ftp://example.com/resource", "url_scheme_unsupported", "http"),
        ("http://127.0.0.1/private", "url_destination_unsafe", "public"),
        ("https://example.com:invalid-port/path", "url_invalid", "valid"),
    ],
)
def test_s7_pr01_url_safety_preflight_fails_closed_before_provider_dispatch(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    input_url: str,
    expected_error: str,
    expected_hint: str,
) -> None:
    outbound_calls = 0

    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        nonlocal outbound_calls
        outbound_calls += 1
        msg = "provider should not be called for safety-preflight failures"
        raise AssertionError(msg)

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "unsafe url": [{"capability_id": "cap.web.extract", "input": {"url": input_url}}]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "unsafe url"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        rendered_message = payload["assistant"]["message"].lower()
        assert expected_error in rendered_message
        assert expected_hint in rendered_message
        assert payload["assistant"]["sources"] == []
        assert outbound_calls == 0


@pytest.mark.parametrize(
    ("intent_case", "expected_error"),
    [
        ("missing", "egress_preflight_missing_intent"),
        ("malformed", "egress_preflight_contract_invalid"),
        ("undeclared", "egress_preflight_undeclared_intent"),
    ],
)
def test_s7_pr01_web_extract_egress_contract_failures_block_before_execute(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    intent_case: str,
    expected_error: str,
) -> None:
    execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal execute_attempts
            execute_attempts += 1
            return capability.execute(input_payload)

        if intent_case == "missing":
            return replace(capability, execute=counted_execute, declare_egress_intent=None)
        if intent_case == "malformed":
            malformed = cast(
                Any,
                lambda _: {"destination": "https://extract.provider.test/v1/extract"},
            )
            return replace(capability, execute=counted_execute, declare_egress_intent=malformed)
        return replace(capability, execute=counted_execute, declare_egress_intent=lambda _: [])

    _patch_capability_lookup(monkeypatch, capability_id="cap.web.extract", mutate=mutate)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "intent failure": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "intent failure"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert expected_error in (attempt["execution"]["error"] or "")
        assert expected_error in payload["assistant"]["message"]
        assert execute_attempts == 0


def test_s7_pr01_non_allowlisted_egress_is_blocked_before_web_extract_execution(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute_attempts = 0

    def mutate(capability: CapabilityDefinition) -> CapabilityDefinition:
        def counted_execute(input_payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal execute_attempts
            execute_attempts += 1
            return capability.execute(input_payload)

        return replace(
            capability,
            execute=counted_execute,
            declare_egress_intent=lambda _: [
                {
                    "destination": "https://evil.example/extract",
                    "payload": {"url": "https://example.com/research/article"},
                }
            ],
        )

    _patch_capability_lookup(monkeypatch, capability_id="cap.web.extract", mutate=mutate)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "egress deny": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "egress deny"})
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert "egress_destination_denied" in (attempt["execution"]["error"] or "")
        assert "allowlist" in payload["assistant"]["message"].lower()
        assert execute_attempts == 0


def test_s7_pr01_transient_provider_failure_retries_are_bounded_and_single_outcome(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT",
        "https://extract.provider.test/v1/extract",
    )
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_MAX_RETRIES", "2")
    call_count = 0

    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        nonlocal call_count
        del args, kwargs
        call_count += 1
        if call_count == 1:
            return _FakeHTTPResponse(status_code=503, payload={"error": "temporary outage"})
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://example.com/research/article",
                content="Final successful extraction content after transient failure.",
                title="Recovered article",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "retry extraction": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "retry extraction"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        assert attempt["execution"]["output"]["provider"]["attempt_count"] == 2
        assert call_count == 2
        assert len(payload["assistant"]["sources"]) == 1
        event_types = _event_types(payload["turn"])
        assert event_types.count("evt.action.execution.succeeded") == 1
        assert event_types.count("evt.action.execution.failed") == 0


def test_s7_pr01_provider_retry_exhaustion_fails_once_with_typed_error(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT",
        "https://extract.provider.test/v1/extract",
    )
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_MAX_RETRIES", "2")
    call_count = 0

    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        nonlocal call_count
        del args, kwargs
        call_count += 1
        return _FakeHTTPResponse(status_code=503, payload={"error": "temporary outage"})

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "retry exhaustion": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "retry exhaustion"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "provider_upstream_failure"
        assert call_count == 3
        rendered_message = payload["assistant"]["message"].lower()
        assert "provider_upstream_failure" in rendered_message
        assert "retry" in rendered_message
        event_types = _event_types(payload["turn"])
        assert event_types.count("evt.action.execution.succeeded") == 0
        assert event_types.count("evt.action.execution.failed") == 1


@pytest.mark.parametrize(
    ("failure_mode", "expected_error", "expected_hint"),
    [
        ("restricted", "access_restricted", "public"),
        ("unsupported", "unsupported_format", "text"),
        ("upstream", "provider_upstream_failure", "retry"),
        ("invalid_payload", "provider_invalid_payload", "retry"),
    ],
)
def test_s7_pr01_typed_url_extraction_failures_are_actionable_and_auditable(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_error: str,
    expected_hint: str,
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        del args, kwargs
        if failure_mode == "restricted":
            return _FakeHTTPResponse(status_code=403, payload={"error": "forbidden"})
        if failure_mode == "unsupported":
            return _FakeHTTPResponse(status_code=415, payload={"error": "unsupported"})
        if failure_mode == "invalid_payload":
            return _FakeHTTPResponse(status_code=200, json_raises=True)
        return _FakeHTTPResponse(status_code=502, payload={"error": "upstream"})

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "failing extraction": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "failing extraction"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == expected_error
        rendered_message = payload["assistant"]["message"].lower()
        assert expected_error in rendered_message
        assert expected_hint in rendered_message


def test_s7_pr01_provider_malformed_final_url_is_fail_closed(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        del args, kwargs
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://example.com:bad-port/research/article",
                content="Provider returned malformed final_url.",
                title="Malformed final url",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "malformed final url": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "malformed final url"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "failed"
        assert attempt["execution"]["error"] == "provider_invalid_payload"
        assert "provider_invalid_payload" in payload["assistant"]["message"]


def test_s7_pr01_public_ipv6_urls_remain_allowed_and_canonical(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT",
        "https://extract.provider.test/v1/extract",
    )

    outbound_urls: list[str] = []

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, Any],
        timeout: float,
    ) -> _FakeHTTPResponse:
        del url, headers, timeout
        outbound_urls.append(json["url"])
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://[2606:4700:4700::1111]/research/article/?utm_source=rss",
                content="IPv6 source extraction evidence.",
                title="IPv6 source",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "ipv6 extraction": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://[2606:4700:4700::1111]/research/article#frag"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "ipv6 extraction"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["canonical_url"] == "https://[2606:4700:4700::1111]/research/article"
        assert output["document"]["canonical_source"] == "https://[2606:4700:4700::1111]/research/article"
        assert output["document"]["resolved_url"] == "https://[2606:4700:4700::1111]/research/article/?utm_source=rss"
        assert outbound_urls == ["https://[2606:4700:4700::1111]/research/article"]


def test_s7_pr01_large_pages_are_bounded_and_partial_disclosure_is_explicit(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    very_large_content = " ".join(["evidence"] * 12000)

    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        del args, kwargs
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://example.com/research/large-page",
                content=very_large_content,
                title="Large page",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "large extraction": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/large-page"},
                }
            ]
        }
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"message": "large extraction"},
        )
        assert sent.status_code == 200
        payload = sent.json()
        attempt = _surface_attempt(payload["turn"])
        assert attempt["execution"]["status"] == "succeeded"
        output = attempt["execution"]["output"]
        assert output["extract_outcome"]["status"] == "partial"
        assert output["extract_outcome"]["reason_code"] == "content_truncated"
        assert output["document"]["truncated"] is True
        assert output["document"]["truncation_reason"] == "content_truncated"
        assert output["document"]["content_chars"] <= 4000
        message = payload["assistant"]["message"].lower()
        assert "partial" in message
        assert "narrow" in message or "focus" in message


def test_s7_pr01_mixed_turn_with_web_extract_preserves_grounding_and_lifecycle_inspectability(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> _FakeHTTPResponse:
        del args, kwargs
        return _FakeHTTPResponse(
            status_code=200,
            payload=_provider_payload(
                final_url="https://example.com/research/article",
                content="Evidence line one. Evidence line two.",
                title="Mixed turn source",
            ),
        )

    monkeypatch.setattr(capability_registry_module.httpx, "post", fake_post)
    adapter = ActionProposalAdapter(
        proposals_by_message={
            "mixed turn": [
                {
                    "capability_id": "cap.web.extract",
                    "input": {"url": "https://example.com/research/article"},
                },
                {"capability_id": "cap.framework.read_echo", "input": {"text": "alpha"}},
            ]
        },
        assistant_text_by_message={
            "mixed turn": "i am fully certain without citing anything.",
        },
    )
    with _build_client(postgres_url, adapter) as client:
        session_id = _session_id(client)
        sent = client.post(f"/v1/sessions/{session_id}/message", json={"message": "mixed turn"})
        assert sent.status_code == 200
        payload = sent.json()
        message = payload["assistant"]["message"].lower()
        assert "[1]" in payload["assistant"]["message"]
        assert "fully certain without citing anything" not in message
        assert len(payload["assistant"]["sources"]) == 1

        lifecycle = payload["turn"]["surface_action_lifecycle"]
        assert len(lifecycle) == 2
        lifecycle_by_capability = {
            item["proposal"]["capability_id"]: item for item in lifecycle if isinstance(item, dict)
        }
        assert lifecycle_by_capability["cap.web.extract"]["execution"]["status"] == "succeeded"
        assert lifecycle_by_capability["cap.framework.read_echo"]["execution"]["status"] == "succeeded"
        assert lifecycle_by_capability["cap.framework.read_echo"]["execution"]["output"] == {"text": "alpha"}
