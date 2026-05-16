from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
import time
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    AIJudgmentRecord,
    ActionAttemptRecord,
    MemoryActionTraceRecord,
    MemoryAssertionEvidenceRecord,
    MemoryAssertionRecord,
    MemoryConflictMemberRecord,
    MemoryConflictSetRecord,
    MemoryContextBlockRecord,
    MemoryDeletionRecord,
    MemoryEmbeddingProjectionRecord,
    MemoryEntityProjectionRecord,
    MemoryEntityRecord,
    MemoryEvalRunRecord,
    MemoryEventRecord,
    MemoryEpisodeRecord,
    MemoryEvidenceRecord,
    MemoryExportArtifactRecord,
    MemoryGraphProjectionRecord,
    MemoryKeywordProjectionRecord,
    MemoryProcedureRecord,
    MemoryProjectionJobRecord,
    MemoryReasoningTraceRecord,
    MemoryRelationshipRecord,
    MemoryReviewRecord,
    MemoryRetentionPolicyRecord,
    MemorySalienceRecord,
    MemoryScopeBindingRecord,
    MemorySensitivityLabelRecord,
    MemorySymbolProjectionRecord,
    MemoryTemporalProjectionRecord,
    MemoryTopicMemberRecord,
    MemoryTopicRecord,
    MemoryVersionRecord,
    ProjectStateSnapshotRecord,
    SessionRecord,
    TurnRecord,
    to_rfc3339,
)
from .redaction import redact_text


MEMORY_CONTEXT_SCHEMA_VERSION = "memory.sota.v2"
MEMORY_PROJECTION_VERSION = "embedding-v1"
MEMORY_CURATION_PROMPT_VERSION = "memory-curation-v2"
MEMORY_CONTINUITY_PROMPT_VERSION = "memory-continuity-v1"
USER_SUBJECT_KEY = "user:default"
ALLOWED_MEMORY_ASSERTION_TYPES = {
    "fact",
    "profile",
    "preference",
    "commitment",
    "decision",
    "project_state",
    "procedure",
    "domain_concept",
    "negative",
}
ALLOWED_MEMORY_REASONING_TRACE_TYPES = {
    "action_path",
    "failure",
    "user_correction",
    "successful_pattern",
    "diagnostic",
}
ALLOWED_MEMORY_REASONING_TRACE_OUTCOMES = {"succeeded", "failed", "corrected", "unknown"}


@dataclass(frozen=True, slots=True)
class PredicateSpec:
    predicate: str
    assertion_type: str
    resolution_policy: str  # "conflict" | "supersede" | "coexist"
    value_kind: str  # "text" | "enum" | "date" | "datetime" | "number" | "json"
    sensitivity_default: str  # MemorySensitivityLabelRecord.label value
    decay_half_life_days: float | None
    enum_values: tuple[str, ...] = ()
    description: str = ""

    @property
    def is_multi_valued(self) -> bool:
        return self.resolution_policy == "coexist"


_DEFAULT_PREDICATE_SPEC = PredicateSpec(
    predicate="*",
    assertion_type="fact",
    resolution_policy="conflict",  # unknown predicates are single-valued: fail safe
    value_kind="text",
    sensitivity_default="personal",
    decay_half_life_days=None,
)

# Closed predicate vocabulary. Type behaviour (cardinality, conflict policy, value
# kind, sensitivity, decay) is deterministic per predicate; the model authors only
# predicate strings and values. Unknown predicates resolve to _DEFAULT_PREDICATE_SPEC.
_PREDICATE_REGISTRY: dict[str, PredicateSpec] = {
    # fact: observed user/world facts; most accumulate, identity-like facts supersede.
    "fact.location": PredicateSpec("fact.location", "fact", "supersede", "text", "personal", None),
    "fact.contact_detail": PredicateSpec(
        "fact.contact_detail", "fact", "supersede", "text", "private", None
    ),
    "fact.relationship": PredicateSpec(
        "fact.relationship", "fact", "coexist", "text", "personal", None
    ),
    "fact.tooling": PredicateSpec("fact.tooling", "fact", "coexist", "text", "public", 180.0),
    "fact.environment": PredicateSpec(
        "fact.environment", "fact", "supersede", "text", "public", 90.0
    ),
    # profile: stable identity attributes; a new value supersedes the old.
    "profile.display_name": PredicateSpec(
        "profile.display_name", "profile", "supersede", "text", "personal", None
    ),
    "profile.role": PredicateSpec(
        "profile.role", "profile", "supersede", "text", "personal", 365.0
    ),
    "profile.timezone": PredicateSpec(
        "profile.timezone", "profile", "supersede", "text", "personal", None
    ),
    "profile.employer": PredicateSpec(
        "profile.employer", "profile", "supersede", "text", "personal", 365.0
    ),
    "profile.pronouns": PredicateSpec(
        "profile.pronouns", "profile", "supersede", "text", "personal", None
    ),
    "profile.location": PredicateSpec(
        "profile.location", "profile", "supersede", "text", "personal", 365.0
    ),
    "profile.expertise": PredicateSpec(
        "profile.expertise", "profile", "coexist", "text", "public", None
    ),
    # preference: how the user wants Ariel to behave.
    "preference.response_verbosity": PredicateSpec(
        "preference.response_verbosity",
        "preference",
        "conflict",
        "enum",
        "personal",
        None,
        enum_values=("terse", "normal", "detailed"),
    ),
    "preference.communication_style": PredicateSpec(
        "preference.communication_style", "preference", "conflict", "text", "personal", None
    ),
    "preference.code_style": PredicateSpec(
        "preference.code_style", "preference", "coexist", "text", "public", None
    ),
    "preference.language": PredicateSpec(
        "preference.language", "preference", "conflict", "text", "personal", None
    ),
    "preference.notification": PredicateSpec(
        "preference.notification", "preference", "conflict", "text", "personal", None
    ),
    "preference.tooling": PredicateSpec(
        "preference.tooling", "preference", "coexist", "text", "public", None
    ),
    "preference.review_depth": PredicateSpec(
        "preference.review_depth",
        "preference",
        "conflict",
        "enum",
        "personal",
        None,
        enum_values=("light", "standard", "thorough"),
    ),
    # commitment: outstanding todos and promises; a scope accumulates many.
    "commitment.todo": PredicateSpec(
        "commitment.todo", "commitment", "coexist", "text", "personal", None
    ),
    "commitment.follow_up": PredicateSpec(
        "commitment.follow_up", "commitment", "coexist", "text", "personal", 30.0
    ),
    "commitment.deadline_promise": PredicateSpec(
        "commitment.deadline_promise", "commitment", "coexist", "datetime", "personal", None
    ),
    "commitment.recurring": PredicateSpec(
        "commitment.recurring", "commitment", "coexist", "text", "personal", None
    ),
    # decision: decisions the user or team made; history is kept, so they coexist.
    "decision.architecture": PredicateSpec(
        "decision.architecture", "decision", "coexist", "text", "public", None
    ),
    "decision.tooling": PredicateSpec(
        "decision.tooling", "decision", "coexist", "text", "public", None
    ),
    "decision.process": PredicateSpec(
        "decision.process", "decision", "coexist", "text", "public", None
    ),
    "decision.scope": PredicateSpec(
        "decision.scope", "decision", "coexist", "text", "public", None
    ),
    "decision.naming": PredicateSpec(
        "decision.naming", "decision", "coexist", "text", "public", None
    ),
    # project_state: live project facts.
    "project.deadline": PredicateSpec(
        "project.deadline", "project_state", "conflict", "text", "personal", None
    ),
    "project.status": PredicateSpec(
        "project.status",
        "project_state",
        "supersede",
        "enum",
        "personal",
        21.0,
        enum_values=("planned", "active", "blocked", "shipped", "abandoned"),
    ),
    "project.priority": PredicateSpec(
        "project.priority",
        "project_state",
        "supersede",
        "enum",
        "personal",
        30.0,
        enum_values=("low", "medium", "high", "critical"),
    ),
    "project.owner": PredicateSpec(
        "project.owner", "project_state", "supersede", "text", "personal", 90.0
    ),
    "project.open_question": PredicateSpec(
        "project.open_question", "project_state", "coexist", "text", "personal", 60.0
    ),
    "project.risk": PredicateSpec(
        "project.risk", "project_state", "coexist", "text", "personal", 60.0
    ),
    "project.blocker": PredicateSpec(
        "project.blocker", "project_state", "coexist", "text", "personal", 30.0
    ),
    "project.milestone": PredicateSpec(
        "project.milestone", "project_state", "coexist", "text", "personal", None
    ),
    # procedure: reusable how-to knowledge; a scope accumulates many.
    "procedure.deploy": PredicateSpec(
        "procedure.deploy", "procedure", "coexist", "text", "public", None
    ),
    "procedure.test": PredicateSpec(
        "procedure.test", "procedure", "coexist", "text", "public", None
    ),
    "procedure.build": PredicateSpec(
        "procedure.build", "procedure", "coexist", "text", "public", None
    ),
    "procedure.release": PredicateSpec(
        "procedure.release", "procedure", "coexist", "text", "public", None
    ),
    "procedure.setup": PredicateSpec(
        "procedure.setup", "procedure", "coexist", "text", "public", None
    ),
    "procedure.debug": PredicateSpec(
        "procedure.debug", "procedure", "coexist", "text", "public", None
    ),
    # domain_concept: definitions and constraints; a new definition supersedes.
    "domain.definition": PredicateSpec(
        "domain.definition", "domain_concept", "supersede", "text", "public", None
    ),
    "domain.glossary_term": PredicateSpec(
        "domain.glossary_term", "domain_concept", "supersede", "text", "public", None
    ),
    "domain.constraint": PredicateSpec(
        "domain.constraint", "domain_concept", "coexist", "text", "public", None
    ),
    "domain.invariant": PredicateSpec(
        "domain.invariant", "domain_concept", "coexist", "text", "public", None
    ),
    # negative: knowledge about what not to do; a scope accumulates many.
    "negative.rejected_approach": PredicateSpec(
        "negative.rejected_approach", "negative", "coexist", "text", "public", 120.0
    ),
    "negative.invalid_assumption": PredicateSpec(
        "negative.invalid_assumption", "negative", "coexist", "text", "public", 120.0
    ),
    "negative.already_checked": PredicateSpec(
        "negative.already_checked", "negative", "coexist", "text", "public", 30.0
    ),
    "negative.unsafe_operation": PredicateSpec(
        "negative.unsafe_operation", "negative", "coexist", "text", "public", None
    ),
    "negative.known_bad_path": PredicateSpec(
        "negative.known_bad_path", "negative", "coexist", "text", "public", 120.0
    ),
}


def resolve_predicate_spec(predicate: str) -> PredicateSpec:
    return _PREDICATE_REGISTRY.get(predicate.strip().lower(), _DEFAULT_PREDICATE_SPEC)


class MemoryValueKindError(ValueError):
    """A candidate value does not match its predicate's declared value kind."""

    code = "E_MEMORY_VALUE_KIND"


class MemoryStaleReasonRequiredError(ValueError):
    """Marking an assertion stale was attempted without a staleness rationale."""

    code = "E_MEMORY_STALE_REASON_REQUIRED"


def _validate_value_kind(spec: PredicateSpec, value: str) -> None:
    """Validate a candidate value against the predicate's declared value kind.

    A value whose malformedness is locally knowable is a rail concern; a
    violation raises MemoryValueKindError, never a silent skip.
    """
    text = value.strip()
    if not text:
        raise MemoryValueKindError("memory value must not be empty")
    if spec.value_kind == "text":
        return
    if spec.value_kind == "enum":
        if text not in spec.enum_values:
            raise MemoryValueKindError(
                f"value {text!r} is not one of {list(spec.enum_values)} "
                f"for predicate {spec.predicate!r}"
            )
        return
    if spec.value_kind in {"date", "datetime"}:
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MemoryValueKindError(
                f"value {text!r} is not a valid {spec.value_kind} for predicate {spec.predicate!r}"
            ) from exc
        return
    if spec.value_kind == "number":
        try:
            float(text)
        except ValueError as exc:
            raise MemoryValueKindError(
                f"value {text!r} is not numeric for predicate {spec.predicate!r}"
            ) from exc
        return
    if spec.value_kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise MemoryValueKindError(
                f"value for predicate {spec.predicate!r} is not valid JSON"
            ) from exc
        return
    raise MemoryValueKindError(f"unknown value kind {spec.value_kind!r}")


class AIJudgmentFailure(RuntimeError):
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


_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "again",
    "be",
    "do",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
}


def count_context_tokens(text: str) -> int:
    """Count tokens the same way the turn pipeline measures ``max_context_tokens``:
    whitespace-delimited words. The hot-index budget reuses this so its limit is
    expressed in the same unit as every other context budget."""
    return len(re.findall(r"\S+", text))


def _clean_text(value: str, *, max_chars: int = 700) -> str:
    return " ".join(value.strip().split())[:max_chars]


def _memory_key(value: str) -> str:
    pieces: list[str] = []
    last_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            pieces.append(char)
            last_was_separator = False
        elif not last_was_separator:
            pieces.append("_")
            last_was_separator = True
    return "".join(pieces).strip("_") or "general"


def _terms(value: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for char in value.lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if token not in _STOPWORDS:
                terms.append(token)
            current = []
    if current:
        token = "".join(current)
        if token not in _STOPWORDS:
            terms.append(token)
    return terms


def _symbol_tokens(value: str) -> list[tuple[str, str]]:
    """Extract (symbol, path) pairs from an assertion's text: whitespace-bounded
    substrings that look like a file path (contain '/') or a code identifier
    (snake_case, interior-capital camelCase, or a dotted name). Deterministic;
    de-duplicated in encounter order."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value.split():
        token = raw.strip("()[]{}<>,;:'\"`.").strip()
        if len(token) < 3 or len(token) > 200:
            continue
        if not all(c.isalnum() or c in "_./-" for c in token):
            continue
        is_path = "/" in token
        has_camel = any(
            token[i].islower() and token[i + 1].isupper() for i in range(len(token) - 1)
        )
        is_identifier = not is_path and ("_" in token or "." in token or has_camel)
        if not (is_path or is_identifier):
            continue
        pair = (token, "") if is_identifier else ("", token)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _memory_version_number(db: Session, *, table: str, record_id: str) -> int:
    return (
        db.scalar(
            select(func.max(MemoryVersionRecord.version)).where(
                MemoryVersionRecord.canonical_table == table,
                MemoryVersionRecord.canonical_id == record_id,
            )
        )
        or 1
    )


@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    allowed: bool
    operation: str  # "recall" | "extract" | "write" | "consolidate"
    effective_mode: str  # "normal" | "temporary" | "no_memory"
    controlling_scope_type: str
    controlling_scope_key: str
    controlling_binding_id: str | None
    reason: str
    considered_scopes: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "operation": self.operation,
            "effective_mode": self.effective_mode,
            "controlling_scope_type": self.controlling_scope_type,
            "controlling_scope_key": self.controlling_scope_key,
            "controlling_binding_id": self.controlling_binding_id,
            "reason": self.reason,
            "considered_scopes": list(self.considered_scopes),
        }


# Mode precedence: a stricter mode in any scope of the chain wins over a laxer one.
_MODE_SEVERITY = {"normal": 0, "temporary": 1, "no_memory": 2}
# Scope specificity decides which carrier of the winning mode is reported; lower is
# more specific. Session is the most specific (severity 0 in the considered list).
_SCOPE_SPECIFICITY = {
    "thread": 10,
    "proactive_case": 20,
    "repo": 30,
    "project": 40,
    "user": 50,
}


def resolve_memory_policy(
    db: Session,
    *,
    operation: str,
    now: datetime,
    session_id: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    project_key: str | None = None,
    repo_key: str | None = None,
    actor_id: str | None = None,
) -> MemoryPolicyDecision:
    """Resolve the effective memory mode for an operation across the full scope
    chain. The strictest mode wins; the most specific scope carrying that mode is
    reported as controlling. Expired bindings are ignored. Operations recall,
    extract, write, and consolidate all require an effective mode of normal."""
    del actor_id  # accepted for call-site symmetry; resolution does not key on it
    considered: list[dict[str, Any]] = []

    # Session scope: mode lives on SessionRecord, not in the bindings table.
    if session_id is not None:
        session = db.get(SessionRecord, session_id)
        if session is None:
            return MemoryPolicyDecision(
                False, operation, "no_memory", "session", session_id, None, "session not found", ()
            )
        considered.append(
            {
                "scope_type": "session",
                "scope_key": session_id,
                "specificity": 0,
                "memory_mode": session.memory_mode,
                "binding_id": None,
            }
        )

    # The other five scope types come from memory_scope_bindings.
    wanted: list[tuple[str, str | None]] = [
        ("thread", thread_id),
        ("proactive_case", proactive_case_id),
        ("repo", repo_key),
        ("project", project_key),
        ("user", USER_SUBJECT_KEY),
    ]
    for scope_type, scope_key in wanted:
        if scope_key is None:
            continue
        binding = db.scalar(
            select(MemoryScopeBindingRecord)
            .where(
                MemoryScopeBindingRecord.scope_type == scope_type,
                MemoryScopeBindingRecord.scope_key == scope_key,
                or_(
                    MemoryScopeBindingRecord.expires_at.is_(None),
                    MemoryScopeBindingRecord.expires_at > now,
                ),
            )
            .order_by(
                MemoryScopeBindingRecord.updated_at.desc(),
                MemoryScopeBindingRecord.id.asc(),
            )
            .limit(1)
        )
        if binding is not None:
            considered.append(
                {
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "specificity": _SCOPE_SPECIFICITY[scope_type],
                    "memory_mode": binding.memory_mode,
                    "binding_id": binding.id,
                }
            )

    if not considered:
        return MemoryPolicyDecision(
            True, operation, "normal", "default", "default", None, "no binding applies", ()
        )

    # Strictest mode wins; the most specific scope carrying it is controlling.
    strictest = max(_MODE_SEVERITY[s["memory_mode"]] for s in considered)
    carriers = [s for s in considered if _MODE_SEVERITY[s["memory_mode"]] == strictest]
    controlling = min(carriers, key=lambda s: s["specificity"])
    mode = controlling["memory_mode"]
    return MemoryPolicyDecision(
        allowed=(mode == "normal"),
        operation=operation,
        effective_mode=mode,
        controlling_scope_type=controlling["scope_type"],
        controlling_scope_key=controlling["scope_key"],
        controlling_binding_id=controlling["binding_id"],
        reason=f"effective mode {mode} from {controlling['scope_type']} scope",
        considered_scopes=tuple(considered),
    )


def scope_keys_for_policy(scope_key: str | None) -> tuple[str | None, str | None]:
    """Split a memory scope_key into the (project_key, repo_key) pair that
    resolve_memory_policy consumes. A bare key (no prefix) is treated as both."""
    if scope_key is None or scope_key in {"global", USER_SUBJECT_KEY, "default"}:
        return None, None
    if scope_key.startswith("project:"):
        return scope_key, None
    if scope_key.startswith("repo:"):
        return None, scope_key
    if scope_key.startswith("proactive:") or scope_key.startswith("session:"):
        return None, None
    return scope_key, scope_key


def _matching_scope_key_for_text(db: Session, *, text: str) -> str | None:
    query_terms = set(_terms(text))
    if not query_terms:
        return None
    now = datetime.now(UTC)
    for binding in db.scalars(
        select(MemoryScopeBindingRecord)
        .where(
            MemoryScopeBindingRecord.scope_type.in_(("project", "repo")),
            (MemoryScopeBindingRecord.expires_at.is_(None))
            | (MemoryScopeBindingRecord.expires_at > now),
        )
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
    ).all():
        if query_terms.intersection(set(_terms(binding.scope_key))):
            return binding.scope_key
    return None


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_value(item) for key, item in value.items()}
    return value


def _never_remember_rule_for_text(
    db: Session,
    *,
    source_session_id: str,
    scope_key: str,
    text: str,
) -> MemoryRetentionPolicyRecord | None:
    text_lower = text.lower()
    for rule in db.scalars(
        select(MemoryRetentionPolicyRecord)
        .where(
            MemoryRetentionPolicyRecord.lifecycle_state == "active",
            MemoryRetentionPolicyRecord.policy_kind == "never_remember",
            MemoryRetentionPolicyRecord.scope_key.in_(
                ("global", scope_key, f"session:{source_session_id}")
            ),
        )
        .order_by(
            MemoryRetentionPolicyRecord.created_at.asc(), MemoryRetentionPolicyRecord.id.asc()
        )
    ).all():
        pattern = rule.pattern.strip().lower()
        if pattern == "*" or (pattern and pattern in text_lower):
            return rule
    return None


def embed_memory_text(text: str, *, settings: AppSettings) -> list[float]:
    if settings.memory_embedding_provider != "openai":
        raise RuntimeError(
            f"unsupported memory embedding provider: {settings.memory_embedding_provider}"
        )
    if settings.openai_api_key is None:
        raise RuntimeError("ARIEL_OPENAI_API_KEY is required for memory embeddings")

    response = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.memory_embedding_model,
            "input": " ".join(text.split()),
            "dimensions": settings.memory_embedding_dimensions,
            "encoding_format": "float",
        },
        timeout=settings.model_timeout_seconds,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"memory embedding request failed: HTTP {exc.response.status_code}"
        ) from exc

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    vector = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(vector, list):
        raise RuntimeError("memory embedding response missing vector")
    if len(vector) != settings.memory_embedding_dimensions:
        raise RuntimeError(
            "memory embedding response dimension mismatch: "
            f"expected {settings.memory_embedding_dimensions}, got {len(vector)}"
        )
    if not all(isinstance(item, int | float) for item in vector):
        raise RuntimeError("memory embedding response vector must be numeric")
    return [float(item) for item in vector]


def _assertion_text(assertion: MemoryAssertionRecord) -> str:
    value = assertion.object_value if isinstance(assertion.object_value, dict) else {}
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _assertion_search_text(assertion: MemoryAssertionRecord) -> str:
    return " ".join(
        (
            assertion.subject_key,
            assertion.predicate,
            assertion.assertion_type,
            _assertion_text(assertion),
        )
    )


def _entity_type(subject_key: str, assertion_type: str) -> str:
    if subject_key.startswith("project:"):
        return "project"
    if subject_key.startswith("repo:"):
        return "repo"
    if assertion_type == "commitment":
        return "commitment"
    if assertion_type == "decision":
        return "decision"
    if assertion_type == "procedure":
        return "procedure"
    if assertion_type == "preference":
        return "preference"
    return "user"


def _ensure_entity(
    db: Session,
    *,
    subject_key: str,
    assertion_type: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEntityRecord:
    entity_type = _entity_type(subject_key, assertion_type)
    entity = db.scalar(
        select(MemoryEntityRecord)
        .where(
            MemoryEntityRecord.entity_type == entity_type,
            MemoryEntityRecord.entity_key == subject_key,
        )
        .limit(1)
    )
    if entity is not None:
        return entity
    entity = MemoryEntityRecord(
        id=new_id_fn("men"),
        entity_type=entity_type,
        entity_key=subject_key,
        display_name=subject_key,
        summary=None,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(entity)
    db.flush()
    return entity


def _record_evidence(
    db: Session,
    *,
    session_id: str,
    turn_id: str | None,
    actor_id: str,
    content_class: str,
    trust_boundary: str,
    source_text: str,
    source_uri: str | None,
    metadata: dict[str, Any],
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryEvidenceRecord:
    text = _clean_text(source_text, max_chars=12_000)
    stored_text = redact_text(text)
    redaction_posture = "redacted" if stored_text != text else "none"
    evidence = MemoryEvidenceRecord(
        id=new_id_fn("mev"),
        source_turn_id=turn_id,
        source_session_id=session_id,
        actor_id=actor_id,
        content_class=content_class,
        trust_boundary=trust_boundary,
        lifecycle_state="available",
        source_uri=source_uri,
        source_artifact_id=None,
        source_text=stored_text,
        evidence_snippet=_clean_text(stored_text, max_chars=360),
        redaction_posture=redaction_posture,
        metadata_json=metadata,
        created_at=now,
        updated_at=now,
    )
    db.add(evidence)
    db.flush()
    _record_version(
        db,
        table="memory_evidence",
        record_id=evidence.id,
        change_type="created",
        actor_id=actor_id,
        reason="memory evidence recorded",
        new_state={
            "source_session_id": session_id,
            "source_turn_id": turn_id,
            "content_class": content_class,
            "trust_boundary": trust_boundary,
            "lifecycle_state": evidence.lifecycle_state,
            "redaction_posture": redaction_posture,
        },
        redaction_posture=redaction_posture,
        now=now,
        new_id_fn=new_id_fn,
    )
    return evidence


def _record_review(
    db: Session,
    *,
    assertion_id: str,
    decision: str,
    actor_id: str,
    reason: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    db.add(
        MemoryReviewRecord(
            id=new_id_fn("mrv"),
            assertion_id=assertion_id,
            decision=decision,
            reason=reason,
            actor_id=actor_id,
            created_at=now,
        )
    )


def _record_version(
    db: Session,
    *,
    table: str,
    record_id: str,
    change_type: str,
    actor_id: str,
    reason: str | None,
    new_state: dict[str, Any] | None = None,
    prior_state: dict[str, Any] | None = None,
    redaction_posture: str = "none",
    now: datetime,
    new_id_fn: Callable[[str], str],
    projection_invalidation: dict[str, Any] | None = None,
) -> None:
    last_version = db.scalar(
        select(MemoryVersionRecord.version)
        .where(
            MemoryVersionRecord.canonical_table == table,
            MemoryVersionRecord.canonical_id == record_id,
        )
        .order_by(MemoryVersionRecord.version.desc())
        .limit(1)
    )
    db.add(
        MemoryVersionRecord(
            id=new_id_fn("mvr"),
            canonical_table=table,
            canonical_id=record_id,
            version=1 if last_version is None else last_version + 1,
            change_type=change_type,
            actor_id=actor_id,
            reason=reason,
            prior_state=prior_state,
            new_state=new_state,
            redaction_posture=redaction_posture,
            projection_invalidation=projection_invalidation or {},
            created_at=now,
        )
    )


def _record_deletion(
    db: Session,
    *,
    target_table: str = "memory_assertions",
    target_id: str,
    deletion_type: str,
    actor_id: str,
    reason: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
    redaction_posture: str = "none",
    projection_invalidation: dict[str, Any] | None = None,
) -> None:
    db.add(
        MemoryDeletionRecord(
            id=new_id_fn("mdl"),
            target_table=target_table,
            target_id=target_id,
            deletion_type=deletion_type,
            actor_id=actor_id,
            reason=reason,
            redaction_posture=redaction_posture,
            projection_invalidation=projection_invalidation or {target_table: target_id},
            created_at=now,
        )
    )


def _event_payload(
    assertion: MemoryAssertionRecord, *, evidence_id: str | None = None
) -> dict[str, Any]:
    payload = {
        "assertion_id": assertion.id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "assertion_type": assertion.assertion_type,
        "lifecycle_state": assertion.lifecycle_state,
        "value_preview": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
    }
    if evidence_id is not None:
        payload["evidence_id"] = evidence_id
    return payload


class MemoryEventError(RuntimeError):
    """A memory mutation produced an event dict that does not match the event
    shape. The producer is the defect; emission must not silently drop it."""


class MemoryProjectionError(RuntimeError):
    """A projection or consolidation job was asked to do something its contract
    forbids (e.g. an unknown consolidation kind). The caller is the defect."""


_EVENT_SUBJECT_REF_KEYS = (
    "assertion_id",
    "assertion_ids",
    "conflict_set_id",
    "resolution_assertion_id",
    "evidence_id",
    "subject_key",
    "predicate",
    "scope_type",
    "scope_key",
    "binding_id",
    "topic_id",
)


def _event_subject_refs(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the subject-reference fields of a memory event payload into the
    indexed subject_refs column. A scope-binding payload carries its row id in
    ``id``; every other payload references subjects by the keys above."""
    refs = {key: payload[key] for key in _EVENT_SUBJECT_REF_KEYS if key in payload}
    if "scope_type" in payload and "id" in payload:
        refs["binding_id"] = payload["id"]
    return refs


def emit_memory_events(
    db: Session,
    *,
    events: Sequence[dict[str, Any]],
    entry_path: str,
    actor_id: str,
    scope_key: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
    source_turn_id: str | None = None,
) -> None:
    """Persist memory mutation events to the non-turn-scoped memory_events log.

    entry_path is one of turn|http|capability|worker|proactive|consolidation.
    A malformed event dict is a defect (MemoryEventError), never a silent drop.
    """
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            raise MemoryEventError(f"malformed memory event: {event!r}")
        db.add(
            MemoryEventRecord(
                id=new_id_fn("mze"),
                event_type=event_type,
                scope_key=scope_key,
                actor_id=actor_id,
                entry_path=entry_path,
                subject_refs=_event_subject_refs(payload),
                payload=payload,
                source_turn_id=source_turn_id,
                created_at=now,
            )
        )


def _active_single_assertions(
    db: Session,
    *,
    subject_entity_id: str,
    predicate: str,
    scope_key: str,
) -> list[MemoryAssertionRecord]:
    return list(
        db.scalars(
            select(MemoryAssertionRecord)
            .where(
                MemoryAssertionRecord.subject_entity_id == subject_entity_id,
                MemoryAssertionRecord.predicate == predicate,
                MemoryAssertionRecord.scope_key == scope_key,
                MemoryAssertionRecord.is_multi_valued.is_(False),
                MemoryAssertionRecord.lifecycle_state == "active",
            )
            .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
        ).all()
    )


def _delete_projection_rows(
    db: Session,
    *,
    assertion_id: str,
    now: datetime,
    redaction_posture: str = "none",
) -> dict[str, Any]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    assertion_text = _assertion_text(assertion).lower() if assertion is not None else ""
    scrub_content = redaction_posture in {"redacted", "privacy_deleted"} and assertion_text != ""
    embedding_ids = list(
        db.scalars(
            select(MemoryEmbeddingProjectionRecord.id).where(
                MemoryEmbeddingProjectionRecord.assertion_id == assertion_id
            )
        ).all()
    )
    projection_job_ids = list(
        db.scalars(
            select(MemoryProjectionJobRecord.id).where(
                MemoryProjectionJobRecord.target_table == "memory_assertions",
                MemoryProjectionJobRecord.target_id == assertion_id,
            )
        ).all()
    )
    keyword_ids = list(
        db.scalars(
            select(MemoryKeywordProjectionRecord.id).where(
                MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
                MemoryKeywordProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    entity_projection_ids = list(
        db.scalars(
            select(MemoryEntityProjectionRecord.id).where(
                MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
                MemoryEntityProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    salience_ids = list(
        db.scalars(
            select(MemorySalienceRecord.id).where(MemorySalienceRecord.assertion_id == assertion_id)
        ).all()
    )
    topic_member_ids = list(
        db.scalars(
            select(MemoryTopicMemberRecord.id).where(
                MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                MemoryTopicMemberRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    source_topic_ids = list(
        db.scalars(
            select(MemoryTopicMemberRecord.topic_id).where(
                MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                MemoryTopicMemberRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    topics_to_delete = [
        topic
        for topic in db.scalars(
            select(MemoryTopicRecord).where(MemoryTopicRecord.lifecycle_state == "active")
        ).all()
        if topic.id in source_topic_ids
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"title": topic.title, "summary": topic.summary, "metadata": topic.metadata_json},
                sort_keys=True,
            ).lower()
        )
    ]
    topic_ids = [topic.id for topic in topics_to_delete]
    temporal_ids = list(
        db.scalars(
            select(MemoryTemporalProjectionRecord.id).where(
                MemoryTemporalProjectionRecord.canonical_table == "memory_assertions",
                MemoryTemporalProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    symbol_ids = list(
        db.scalars(
            select(MemorySymbolProjectionRecord.id).where(
                MemorySymbolProjectionRecord.canonical_table == "memory_assertions",
                MemorySymbolProjectionRecord.canonical_id == assertion_id,
            )
        ).all()
    )
    graph_ids: list[str] = []
    if assertion is not None:
        graph_ids = list(
            db.scalars(
                select(MemoryGraphProjectionRecord.id).where(
                    (MemoryGraphProjectionRecord.source_entity_id == assertion.subject_entity_id)
                    | (MemoryGraphProjectionRecord.target_entity_id == assertion.subject_entity_id)
                )
            ).all()
        )
    procedures_to_delete = [
        procedure
        for procedure in db.scalars(
            select(MemoryProcedureRecord).where(MemoryProcedureRecord.lifecycle_state == "active")
        ).all()
        if procedure.source_assertion_id == assertion_id
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"title": procedure.title, "instruction": procedure.instruction},
                sort_keys=True,
            ).lower()
        )
    ]
    procedure_ids = [procedure.id for procedure in procedures_to_delete]
    snapshots_to_delete = [
        snapshot
        for snapshot in db.scalars(
            select(ProjectStateSnapshotRecord).where(
                ProjectStateSnapshotRecord.lifecycle_state == "active"
            )
        ).all()
        if assertion_id in snapshot.source_assertion_ids
        or (
            scrub_content
            and assertion_text
            in json.dumps(
                {"summary": snapshot.summary, "state": snapshot.state},
                sort_keys=True,
            ).lower()
        )
    ]
    snapshot_ids = [snapshot.id for snapshot in snapshots_to_delete]
    blocks_to_delete = [
        block
        for block in db.scalars(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.lifecycle_state == "active"
            )
        ).all()
        if assertion_id in block.source_assertion_ids
        or (scrub_content and assertion_text in block.content.lower())
    ]
    context_block_ids = [block.id for block in blocks_to_delete]
    export_artifact_ids: list[str] = []
    for artifact in db.scalars(select(MemoryExportArtifactRecord)).all():
        artifact_content = json.dumps(artifact.content, sort_keys=True)
        if assertion_id not in artifact_content and not (
            scrub_content and assertion_text in artifact_content.lower()
        ):
            continue
        export_artifact_ids.append(artifact.id)
        artifact.status = "failed"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            artifact.content = {}
            artifact.redaction_posture = redaction_posture
        artifact.source_counts = {
            **artifact.source_counts,
            "invalidated_by_assertion_id": assertion_id,
            "invalidated_at": to_rfc3339(now),
            "redaction_posture": redaction_posture,
        }
        artifact.updated_at = now

    db.execute(
        delete(MemoryEmbeddingProjectionRecord).where(
            MemoryEmbeddingProjectionRecord.assertion_id == assertion_id
        )
    )
    db.execute(
        delete(MemoryProjectionJobRecord).where(
            MemoryProjectionJobRecord.target_table == "memory_assertions",
            MemoryProjectionJobRecord.target_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryKeywordProjectionRecord).where(
            MemoryKeywordProjectionRecord.canonical_table == "memory_assertions",
            MemoryKeywordProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryEntityProjectionRecord).where(
            MemoryEntityProjectionRecord.canonical_table == "memory_assertions",
            MemoryEntityProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemorySalienceRecord).where(MemorySalienceRecord.assertion_id == assertion_id)
    )
    db.execute(
        delete(MemoryTopicMemberRecord).where(
            MemoryTopicMemberRecord.canonical_table == "memory_assertions",
            MemoryTopicMemberRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemoryTemporalProjectionRecord).where(
            MemoryTemporalProjectionRecord.canonical_table == "memory_assertions",
            MemoryTemporalProjectionRecord.canonical_id == assertion_id,
        )
    )
    db.execute(
        delete(MemorySymbolProjectionRecord).where(
            MemorySymbolProjectionRecord.canonical_table == "memory_assertions",
            MemorySymbolProjectionRecord.canonical_id == assertion_id,
        )
    )
    if assertion is not None:
        db.execute(
            delete(MemoryGraphProjectionRecord).where(
                (MemoryGraphProjectionRecord.source_entity_id == assertion.subject_entity_id)
                | (MemoryGraphProjectionRecord.target_entity_id == assertion.subject_entity_id)
            )
        )
    for procedure in procedures_to_delete:
        procedure.lifecycle_state = "deleted"
        procedure.valid_to = now
        if redaction_posture in {"redacted", "privacy_deleted"}:
            procedure.title = f"[{redaction_posture}]"
            procedure.instruction = f"[{redaction_posture}]"
            procedure.metadata_json = {
                **procedure.metadata_json,
                "redaction_posture": redaction_posture,
            }
        procedure.updated_at = now
    for snapshot in snapshots_to_delete:
        snapshot.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            snapshot.summary = f"[{redaction_posture}]"
            snapshot.state = {"redaction_posture": redaction_posture}
        snapshot.updated_at = now
    for block in blocks_to_delete:
        block.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            block.content = f"[{redaction_posture}]"
        block.updated_at = now
    for topic in topics_to_delete:
        topic.lifecycle_state = "deleted"
        if redaction_posture in {"redacted", "privacy_deleted"}:
            topic.title = f"[{redaction_posture}]"
            topic.summary = f"[{redaction_posture}]"
            topic.metadata_json = {"redaction_posture": redaction_posture}
        topic.updated_at = now
    invalidation = {
        "assertion_id": assertion_id,
        "deleted_rows": {
            "memory_embedding_projections": embedding_ids,
            "memory_projection_jobs": projection_job_ids,
            "memory_keyword_projections": keyword_ids,
            "memory_entity_projections": entity_projection_ids,
            "memory_salience": salience_ids,
            "memory_topic_members": topic_member_ids,
            "memory_temporal_projections": temporal_ids,
            "memory_symbol_projections": symbol_ids,
            "memory_graph_projections": graph_ids,
        },
        "marked_deleted": {
            "memory_procedures": procedure_ids,
            "project_state_snapshots": snapshot_ids,
            "memory_context_blocks": context_block_ids,
            "memory_topics": topic_ids,
        },
        "invalidated_exports": export_artifact_ids,
    }
    return invalidation


def _record_projection_rows(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    subject_entity_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    source_memory_version = _memory_version_number(
        db, table="memory_assertions", record_id=assertion.id
    )
    search_text = _assertion_search_text(assertion)
    db.add(
        MemoryKeywordProjectionRecord(
            id=new_id_fn("mkp"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            projection_version=MEMORY_PROJECTION_VERSION,
            source_memory_version=source_memory_version,
            search_text=search_text,
            search_document=search_text,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryEntityProjectionRecord(
            id=new_id_fn("mep"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            entity_id=subject_entity_id,
            projection_version=MEMORY_PROJECTION_VERSION,
            source_memory_version=source_memory_version,
            mention_text=assertion.subject_key,
            features={"role": "subject"},
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryProjectionJobRecord(
            id=new_id_fn("mpj"),
            projection_kind="embedding",
            target_table="memory_assertions",
            target_id=assertion.id,
            lifecycle_state="pending",
            attempts=0,
            max_retries=3,
            error=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        MemoryTemporalProjectionRecord(
            id=new_id_fn("mtpj"),
            canonical_table="memory_assertions",
            canonical_id=assertion.id,
            temporal_kind="validity",
            valid_from=assertion.valid_from,
            valid_to=assertion.valid_to,
            occurred_at=None,
            projection_version=MEMORY_PROJECTION_VERSION,
            source_memory_version=source_memory_version,
            metadata_json={"source": "assertion_validity"},
            created_at=now,
            updated_at=now,
        )
    )
    # Symbol projection: only repo-scoped assertions carry code identifiers and
    # paths worth indexing for the symbol retrieval signal.
    repo_key = ""
    if assertion.scope_key.startswith("repo:"):
        repo_key = assertion.scope_key
    elif assertion.subject_key.startswith("repo:"):
        repo_key = assertion.subject_key
    if repo_key:
        for symbol, path in _symbol_tokens(_assertion_text(assertion)):
            db.add(
                MemorySymbolProjectionRecord(
                    id=new_id_fn("msy"),
                    canonical_table="memory_assertions",
                    canonical_id=assertion.id,
                    repo_key=repo_key,
                    symbol=symbol,
                    path=path,
                    language=None,
                    projection_version=MEMORY_PROJECTION_VERSION,
                    source_memory_version=source_memory_version,
                    metadata_json={"source": "assertion_text"},
                    created_at=now,
                    updated_at=now,
                )
            )


def process_memory_projection_job(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    worker_id: str = "memory-worker",
) -> bool:
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.scalar(
                select(MemoryProjectionJobRecord)
                .where(
                    MemoryProjectionJobRecord.projection_kind == "embedding",
                    MemoryProjectionJobRecord.lifecycle_state == "pending",
                    MemoryProjectionJobRecord.run_after <= now,
                )
                .order_by(
                    MemoryProjectionJobRecord.run_after.asc(),
                    MemoryProjectionJobRecord.created_at.asc(),
                    MemoryProjectionJobRecord.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return False

            job.lifecycle_state = "running"
            job.attempts += 1
            job.claimed_by = worker_id
            job.attempt_token = new_id_fn("mpa")
            job.last_heartbeat = now
            job.updated_at = now
            job_id = job.id
            attempt_token = str(job.attempt_token)
            if job.target_table != "memory_assertions":
                job.lifecycle_state = "dead_letter"
                job.error = f"malformed embedding projection target table: {job.target_table}"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            assertion = db.get(MemoryAssertionRecord, job.target_id)
            if assertion is None:
                job.lifecycle_state = "dead_letter"
                job.error = "malformed embedding projection missing assertion"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            if assertion.lifecycle_state != "active":
                job.lifecycle_state = "completed"
                job.error = None
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.updated_at = now
                return True
            assertion_id = assertion.id
            search_text = _assertion_search_text(assertion)

    try:
        vector = embed_memory_text(search_text, settings=settings)
    except Exception as exc:
        with session_factory() as db:
            with db.begin():
                job = db.scalar(
                    select(MemoryProjectionJobRecord)
                    .where(
                        MemoryProjectionJobRecord.id == job_id,
                        MemoryProjectionJobRecord.lifecycle_state == "running",
                        MemoryProjectionJobRecord.attempt_token == attempt_token,
                    )
                    .with_for_update()
                )
                if job is not None:
                    now = now_fn()
                    job.lifecycle_state = (
                        "dead_letter" if job.attempts >= job.max_retries else "pending"
                    )
                    job.error = _clean_text(str(exc), max_chars=500)
                    job.claimed_by = None
                    job.attempt_token = None
                    job.last_heartbeat = None
                    job.run_after = (
                        now if job.lifecycle_state == "dead_letter" else now + timedelta(seconds=30)
                    )
                    job.updated_at = now
        return True

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.scalar(
                select(MemoryProjectionJobRecord)
                .where(
                    MemoryProjectionJobRecord.id == job_id,
                    MemoryProjectionJobRecord.lifecycle_state == "running",
                    MemoryProjectionJobRecord.attempt_token == attempt_token,
                )
                .with_for_update()
            )
            if job is None:
                return True
            assertion = db.get(MemoryAssertionRecord, assertion_id)
            if assertion is None or assertion.lifecycle_state != "active":
                job.lifecycle_state = "completed"
                job.error = None
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.updated_at = now
                return True

            search_text = _assertion_search_text(assertion)
            source_memory_version = (
                db.scalar(
                    select(func.max(MemoryVersionRecord.version)).where(
                        MemoryVersionRecord.canonical_table == "memory_assertions",
                        MemoryVersionRecord.canonical_id == assertion.id,
                    )
                )
                or 1
            )
            row = db.scalar(
                select(MemoryEmbeddingProjectionRecord)
                .where(
                    MemoryEmbeddingProjectionRecord.assertion_id == assertion.id,
                    MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                )
                .limit(1)
            )
            if row is None:
                db.add(
                    MemoryEmbeddingProjectionRecord(
                        id=new_id_fn("mep"),
                        assertion_id=assertion.id,
                        projection_version=MEMORY_PROJECTION_VERSION,
                        source_memory_version=source_memory_version,
                        embedding_provider=settings.memory_embedding_provider,
                        embedding_model=settings.memory_embedding_model,
                        embedding_dimensions=settings.memory_embedding_dimensions,
                        search_text=search_text,
                        embedding=vector,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.embedding_provider = settings.memory_embedding_provider
                row.embedding_model = settings.memory_embedding_model
                row.embedding_dimensions = settings.memory_embedding_dimensions
                row.source_memory_version = source_memory_version
                row.search_text = search_text
                row.embedding = vector
                row.updated_at = now

            job.lifecycle_state = "completed"
            job.error = None
            job.claimed_by = None
            job.attempt_token = None
            job.last_heartbeat = None
            job.updated_at = now
    return True


_GRAPH_PROJECTION_MAX_DISTANCE = 3


def _rebuild_graph_projection(
    db: Session,
    *,
    source_entity_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    """Recompute the BFS reachability of one source entity to depth 3 over the
    active relationship graph, replacing its memory_graph_projections rows."""
    edges: dict[str, list[MemoryRelationshipRecord]] = {}
    for relationship in db.scalars(
        select(MemoryRelationshipRecord)
        .where(MemoryRelationshipRecord.lifecycle_state == "active")
        .order_by(MemoryRelationshipRecord.id.asc())
    ).all():
        edges.setdefault(relationship.source_entity_id, []).append(relationship)
    db.execute(
        delete(MemoryGraphProjectionRecord).where(
            MemoryGraphProjectionRecord.source_entity_id == source_entity_id
        )
    )
    reached: dict[str, tuple[int, list[dict[str, Any]], dict[str, int], float]] = {}
    frontier: list[tuple[str, int, list[dict[str, Any]], dict[str, int], float]] = [
        (source_entity_id, 0, [], {}, 1.0)
    ]
    while frontier:
        entity_id, distance, path, versions, score = frontier.pop(0)
        if distance >= _GRAPH_PROJECTION_MAX_DISTANCE:
            continue
        for relationship in edges.get(entity_id, []):
            target_id = relationship.target_entity_id
            if target_id == source_entity_id:
                continue
            next_distance = distance + 1
            next_path = [
                *path,
                {
                    "relationship_id": relationship.id,
                    "relationship_type": relationship.relationship_type,
                },
            ]
            next_versions = {**versions, relationship.id: 1}
            next_score = score * relationship.confidence
            existing = reached.get(target_id)
            if existing is not None and existing[0] <= next_distance:
                continue
            reached[target_id] = (next_distance, next_path, next_versions, next_score)
            frontier.append((target_id, next_distance, next_path, next_versions, next_score))
    for target_id, (distance, path, versions, score) in sorted(reached.items()):
        db.add(
            MemoryGraphProjectionRecord(
                id=new_id_fn("mgp"),
                source_entity_id=source_entity_id,
                target_entity_id=target_id,
                projection_version=MEMORY_PROJECTION_VERSION,
                source_memory_versions={"memory_relationships": versions},
                source_projection_versions={"memory_graph_projections": MEMORY_PROJECTION_VERSION},
                relationship_path=path,
                distance=distance,
                score=score,
                created_at=now,
                updated_at=now,
            )
        )


def process_memory_graph_projection_job(
    *,
    session_factory: sessionmaker[Session],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    worker_id: str = "memory-worker",
) -> bool:
    """Claim and run one pending graph projection job, rebuilding the BFS
    reachability of its target source entity. One SERIALIZABLE transaction."""
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            job = db.scalar(
                select(MemoryProjectionJobRecord)
                .where(
                    MemoryProjectionJobRecord.projection_kind == "graph",
                    MemoryProjectionJobRecord.lifecycle_state == "pending",
                    MemoryProjectionJobRecord.run_after <= now,
                )
                .order_by(
                    MemoryProjectionJobRecord.run_after.asc(),
                    MemoryProjectionJobRecord.created_at.asc(),
                    MemoryProjectionJobRecord.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return False
            job.lifecycle_state = "running"
            job.attempts += 1
            job.claimed_by = worker_id
            job.attempt_token = new_id_fn("mpa")
            job.last_heartbeat = now
            job.updated_at = now
            if job.target_table != "memory_entities":
                job.lifecycle_state = "dead_letter"
                job.error = f"malformed graph projection target table: {job.target_table}"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            entity = db.get(MemoryEntityRecord, job.target_id)
            if entity is None:
                job.lifecycle_state = "dead_letter"
                job.error = "malformed graph projection missing source entity"
                job.claimed_by = None
                job.attempt_token = None
                job.last_heartbeat = None
                job.run_after = now
                job.updated_at = now
                return True
            _rebuild_graph_projection(db, source_entity_id=entity.id, now=now, new_id_fn=new_id_fn)
            job.lifecycle_state = "completed"
            job.error = None
            job.claimed_by = None
            job.attempt_token = None
            job.last_heartbeat = None
            job.updated_at = now
    return True


def _record_salience(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    user_priority: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    row = db.scalar(
        select(MemorySalienceRecord)
        .where(MemorySalienceRecord.assertion_id == assertion.id)
        .limit(1)
    )
    signals = {
        "assertion_type": assertion.assertion_type,
        "confidence": assertion.confidence,
        "user_priority": user_priority,
    }
    if row is None:
        db.add(
            MemorySalienceRecord(
                id=new_id_fn("msl"),
                assertion_id=assertion.id,
                user_priority=user_priority,
                score=0.0,
                signals=signals,
                created_at=now,
                updated_at=now,
            )
        )
        return
    row.user_priority = user_priority
    row.score = 0.0
    row.signals = signals
    row.updated_at = now


def _record_project_snapshot(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if assertion.assertion_type != "project_state":
        return
    text = redact_text(_assertion_text(assertion))
    source_evidence_ids = list(
        db.scalars(
            select(MemoryAssertionEvidenceRecord.evidence_id).where(
                MemoryAssertionEvidenceRecord.assertion_id == assertion.id
            )
        ).all()
    )
    source_versions = {
        "memory_assertions": {
            assertion.id: _memory_version_number(
                db, table="memory_assertions", record_id=assertion.id
            )
        }
    }
    snapshot = ProjectStateSnapshotRecord(
        id=new_id_fn("pss"),
        project_key=assertion.subject_key.removeprefix("project:"),
        summary=text,
        state={
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "value": text,
        },
        source_assertion_ids=[assertion.id],
        source_episode_ids=[],
        source_evidence_ids=source_evidence_ids,
        lifecycle_state="active",
        projection_version=MEMORY_PROJECTION_VERSION,
        created_at=now,
        updated_at=now,
    )
    db.add(snapshot)
    topic_key = f"{assertion.subject_key}:project-state"
    topic = db.scalar(
        select(MemoryTopicRecord)
        .where(
            MemoryTopicRecord.scope_key == assertion.subject_key,
            MemoryTopicRecord.topic_key == topic_key,
        )
        .limit(1)
    )
    if topic is None:
        topic = MemoryTopicRecord(
            id=new_id_fn("mtp"),
            topic_key=topic_key,
            family="active-projects",
            scope_key=assertion.subject_key,
            title=f"{assertion.subject_key} project state",
            summary=text,
            lifecycle_state="active",
            projection_version=MEMORY_PROJECTION_VERSION,
            metadata_json={"subject_key": assertion.subject_key},
            created_at=now,
            updated_at=now,
        )
        db.add(topic)
        db.flush()
    else:
        topic.summary = text
        topic.lifecycle_state = "active"
        topic.projection_version = MEMORY_PROJECTION_VERSION
        topic.updated_at = now
    topic_member = db.scalar(
        select(MemoryTopicMemberRecord)
        .where(
            MemoryTopicMemberRecord.topic_id == topic.id,
            MemoryTopicMemberRecord.canonical_table == "memory_assertions",
            MemoryTopicMemberRecord.canonical_id == assertion.id,
        )
        .limit(1)
    )
    if topic_member is None:
        db.add(
            MemoryTopicMemberRecord(
                id=new_id_fn("mtm"),
                topic_id=topic.id,
                canonical_table="memory_assertions",
                canonical_id=assertion.id,
                membership_kind="source",
                rank=0,
                metadata_json={"assertion_type": assertion.assertion_type},
                created_at=now,
            )
        )
    block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "project_state",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="project_state",
                scope_key=assertion.subject_key,
                content=f"{assertion.subject_key}: {text}",
                topic_id=None,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        block.content = f"{assertion.subject_key}: {text}"
        block.lifecycle_state = "active"
        block.source_assertion_ids = [assertion.id]
        block.source_project_state_snapshot_ids = [snapshot.id]
        block.source_memory_versions = source_versions
        block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        block.updated_at = now
    hot_block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if hot_block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="hot_index",
                scope_key=assertion.subject_key,
                content=f"project state: {assertion.subject_key}: {text}",
                topic_id=None,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        hot_block.content = f"project state: {assertion.subject_key}: {text}"
        hot_block.lifecycle_state = "active"
        hot_block.source_assertion_ids = [assertion.id]
        hot_block.source_project_state_snapshot_ids = [snapshot.id]
        hot_block.source_memory_versions = source_versions
        hot_block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        hot_block.updated_at = now
    topic_block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "topic",
            MemoryContextBlockRecord.scope_key == assertion.subject_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if topic_block is None:
        db.add(
            MemoryContextBlockRecord(
                id=new_id_fn("mcb"),
                block_type="topic",
                scope_key=assertion.subject_key,
                content=text,
                topic_id=topic.id,
                lifecycle_state="active",
                source_assertion_ids=[assertion.id],
                source_episode_ids=[],
                source_trace_ids=[],
                source_action_trace_ids=[],
                source_procedure_ids=[],
                source_project_state_snapshot_ids=[snapshot.id],
                source_memory_versions=source_versions,
                source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                projection_version=MEMORY_PROJECTION_VERSION,
                created_at=now,
                updated_at=now,
            )
        )
        return
    topic_block.content = text
    topic_block.topic_id = topic.id
    topic_block.lifecycle_state = "active"
    topic_block.source_assertion_ids = [assertion.id]
    topic_block.source_project_state_snapshot_ids = [snapshot.id]
    topic_block.source_memory_versions = source_versions
    topic_block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
    topic_block.updated_at = now


def _record_procedure(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    evidence_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> None:
    if assertion.assertion_type != "procedure":
        return
    procedure_key = _memory_key(assertion.predicate)
    procedure = db.scalar(
        select(MemoryProcedureRecord)
        .where(
            MemoryProcedureRecord.procedure_key == procedure_key,
            MemoryProcedureRecord.scope_key == assertion.scope_key,
        )
        .limit(1)
    )
    if procedure is None:
        db.add(
            MemoryProcedureRecord(
                id=new_id_fn("mpr"),
                procedure_key=procedure_key,
                scope_key=assertion.scope_key,
                title=assertion.predicate,
                instruction=_assertion_text(assertion),
                lifecycle_state="active",
                review_state="approved",
                source_assertion_id=assertion.id,
                primary_evidence_id=evidence_id,
                valid_from=assertion.valid_from,
                valid_to=assertion.valid_to,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )
        return
    procedure.title = assertion.predicate
    procedure.instruction = _assertion_text(assertion)
    procedure.lifecycle_state = "active"
    procedure.review_state = "approved"
    procedure.source_assertion_id = assertion.id
    procedure.primary_evidence_id = evidence_id
    procedure.valid_from = assertion.valid_from
    procedure.valid_to = assertion.valid_to
    procedure.updated_at = now


def _create_assertion(
    db: Session,
    *,
    entity: MemoryEntityRecord,
    evidence: MemoryEvidenceRecord,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    confidence: float,
    scope_key: str,
    is_multi_valued: bool,
    lifecycle_state: str,
    valid_from: datetime | None,
    valid_to: datetime | None,
    extraction_model: str | None,
    extraction_prompt_version: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryAssertionRecord:
    assertion = MemoryAssertionRecord(
        id=new_id_fn("mas"),
        subject_entity_id=entity.id,
        subject_key=subject_key,
        predicate=predicate,
        scope_key=scope_key,
        object_value={"text": _clean_text(value)},
        assertion_type=assertion_type,
        is_multi_valued=is_multi_valued,
        scope={"kind": "global", "key": scope_key},
        lifecycle_state=lifecycle_state,
        confidence=max(0.0, min(confidence, 1.0)),
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_by_assertion_id=None,
        extraction_model=extraction_model,
        extraction_prompt_version=extraction_prompt_version,
        last_verified_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(assertion)
    db.flush()
    db.add(
        MemoryAssertionEvidenceRecord(
            id=new_id_fn("mae"),
            assertion_id=assertion.id,
            evidence_id=evidence.id,
            created_at=now,
        )
    )
    return assertion


def _open_conflict(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    active_assertions: Sequence[MemoryAssertionRecord],
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    conflict = db.scalar(
        select(MemoryConflictSetRecord)
        .where(
            MemoryConflictSetRecord.subject_entity_id == assertion.subject_entity_id,
            MemoryConflictSetRecord.predicate == assertion.predicate,
            MemoryConflictSetRecord.scope_key == assertion.scope_key,
            MemoryConflictSetRecord.lifecycle_state == "open",
        )
        .limit(1)
    )
    if conflict is None:
        conflict = MemoryConflictSetRecord(
            id=new_id_fn("mcf"),
            subject_entity_id=assertion.subject_entity_id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
            lifecycle_state="open",
            conflict_type="value_contradiction",
            resolution_assertion_id=None,
            reason="candidate contradicts active memory",
            created_at=now,
            updated_at=now,
        )
        db.add(conflict)
        db.flush()

    assertion_prior_state = {"lifecycle_state": assertion.lifecycle_state}
    assertion.lifecycle_state = "conflicted"
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate entered open conflict",
        prior_state=assertion_prior_state,
        new_state={"lifecycle_state": "conflicted"},
        now=now,
        new_id_fn=new_id_fn,
    )
    for member in [assertion, *active_assertions]:
        exists = db.scalar(
            select(MemoryConflictMemberRecord)
            .where(
                MemoryConflictMemberRecord.conflict_set_id == conflict.id,
                MemoryConflictMemberRecord.assertion_id == member.id,
            )
            .limit(1)
        )
        if exists is None:
            db.add(
                MemoryConflictMemberRecord(
                    id=new_id_fn("mcm"),
                    conflict_set_id=conflict.id,
                    assertion_id=member.id,
                    created_at=now,
                )
            )
        if member.id != assertion.id and member.lifecycle_state == "active":
            prior_state = {"lifecycle_state": member.lifecycle_state}
            member.updated_at = now
            projection_invalidation = _delete_projection_rows(db, assertion_id=member.id, now=now)
            _record_version(
                db,
                table="memory_assertions",
                record_id=member.id,
                change_type="updated",
                actor_id=actor_id,
                reason="active memory projection invalidated by open conflict",
                prior_state=prior_state,
                new_state={"lifecycle_state": "active", "conflict_set_id": conflict.id},
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
            )

    return {
        "event_type": "evt.memory.conflict_opened",
        "payload": {
            "conflict_set_id": conflict.id,
            "subject_key": assertion.subject_key,
            "predicate": assertion.predicate,
            "assertion_ids": [assertion.id, *[item.id for item in active_assertions]],
        },
    }


def _activate_assertion(
    db: Session,
    *,
    assertion: MemoryAssertionRecord,
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not assertion.is_multi_valued:
        for existing in _active_single_assertions(
            db,
            subject_entity_id=assertion.subject_entity_id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
        ):
            if existing.id == assertion.id:
                continue
            existing.lifecycle_state = "superseded"
            existing.superseded_by_assertion_id = assertion.id
            existing.valid_to = now
            existing.updated_at = now
            projection_invalidation = _delete_projection_rows(db, assertion_id=existing.id, now=now)
            _record_version(
                db,
                table="memory_assertions",
                record_id=existing.id,
                change_type="superseded",
                actor_id=actor_id,
                reason="single-valued assertion replaced",
                new_state={"superseded_by_assertion_id": assertion.id},
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
            )
            events.append(
                {
                    "event_type": "evt.memory.assertion_superseded",
                    "payload": _event_payload(existing),
                }
            )

    evidence_id = db.scalar(
        select(MemoryAssertionEvidenceRecord.evidence_id)
        .where(MemoryAssertionEvidenceRecord.assertion_id == assertion.id)
        .order_by(MemoryAssertionEvidenceRecord.created_at.asc())
        .limit(1)
    )
    assertion.lifecycle_state = "active"
    assertion.valid_from = assertion.valid_from or now
    assertion.last_verified_at = now
    assertion.updated_at = now
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="approved",
        actor_id=actor_id,
        reason="reviewed memory activated",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="reviewed",
        actor_id=actor_id,
        reason="activated",
        new_state={"lifecycle_state": "active"},
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_projection_rows(
        db,
        assertion=assertion,
        subject_entity_id=assertion.subject_entity_id,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_salience(
        db,
        assertion=assertion,
        user_priority="none",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_project_snapshot(db, assertion=assertion, now=now, new_id_fn=new_id_fn)
    if evidence_id is not None:
        _record_procedure(
            db,
            assertion=assertion,
            evidence_id=evidence_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    events.append(
        {"event_type": "evt.memory.assertion_activated", "payload": _event_payload(assertion)}
    )
    events.append(
        {
            "event_type": "evt.memory.projection_rebuilt",
            "payload": {
                "assertion_id": assertion.id,
                "projection_version": MEMORY_PROJECTION_VERSION,
                "projection_kinds": ["keyword", "entity", "salience"],
                "queued_projection_kinds": ["embedding"],
            },
        }
    )
    return events


_LIVE_CONFLICT_MEMBER_STATES = {"active", "candidate", "conflicted"}


def _settle_conflict_sets_for_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Re-evaluate every open conflict set the assertion belongs to and close any
    that no longer has a live contradiction. A conflict with one live member
    resolves toward it (activating it if needed); one with no live member is
    ignored. Conflict sets always reach a terminal state. Returns event dicts."""
    events: list[dict[str, Any]] = []
    set_ids = db.scalars(
        select(MemoryConflictMemberRecord.conflict_set_id).where(
            MemoryConflictMemberRecord.assertion_id == assertion_id
        )
    ).all()
    for set_id in set_ids:
        conflict = db.get(MemoryConflictSetRecord, set_id)
        if conflict is None or conflict.lifecycle_state != "open":
            continue
        member_ids = db.scalars(
            select(MemoryConflictMemberRecord.assertion_id).where(
                MemoryConflictMemberRecord.conflict_set_id == set_id
            )
        ).all()
        live = [
            member
            for member in (db.get(MemoryAssertionRecord, mid) for mid in member_ids)
            if member is not None and member.lifecycle_state in _LIVE_CONFLICT_MEMBER_STATES
        ]
        if len(live) >= 2:
            continue  # contradiction still live: stays open
        if len(live) == 1:
            winner = live[0]
            conflict.lifecycle_state = "resolved"
            conflict.resolution_assertion_id = winner.id
            if winner.lifecycle_state != "active":
                events.extend(
                    _activate_assertion(
                        db, assertion=winner, actor_id=actor_id, now=now, new_id_fn=new_id_fn
                    )
                )
        else:  # contradiction evaporated entirely
            conflict.lifecycle_state = "ignored"
        conflict.updated_at = now
        _record_version(
            db,
            table="memory_conflict_sets",
            record_id=conflict.id,
            change_type="reviewed",
            actor_id=actor_id,
            reason="conflict settled by member lifecycle change",
            new_state={"lifecycle_state": conflict.lifecycle_state},
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {
                "event_type": "evt.memory.conflict_resolved",
                "payload": {
                    "conflict_set_id": conflict.id,
                    "lifecycle_state": conflict.lifecycle_state,
                    "resolution_assertion_id": conflict.resolution_assertion_id,
                },
            }
        )
    return events


def record_turn_memory_evidence(
    db: Session,
    *,
    session_id: str,
    source_turn_id: str,
    user_message: str,
    assistant_message: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    thread_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    now = now_fn()
    project_key, repo_key = scope_keys_for_policy(
        _matching_scope_key_for_text(db, text=user_message)
    )
    if not resolve_memory_policy(
        db,
        operation="write",
        now=now,
        session_id=session_id,
        thread_id=thread_id,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    ).allowed:
        return [], None
    user_evidence = _record_evidence(
        db,
        session_id=session_id,
        turn_id=source_turn_id,
        actor_id=actor_id,
        content_class="user_message",
        trust_boundary="trusted_user",
        source_text=user_message,
        source_uri=None,
        metadata={"capture_mode": "turn_evidence"},
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {
            "event_type": "evt.memory.evidence_recorded",
            "payload": {
                "evidence_id": user_evidence.id,
                "source_turn_id": source_turn_id,
                "source_session_id": session_id,
                "content_class": user_evidence.content_class,
                "trust_boundary": user_evidence.trust_boundary,
            },
        }
    ]
    if assistant_message:
        assistant_evidence = _record_evidence(
            db,
            session_id=session_id,
            turn_id=source_turn_id,
            actor_id="assistant",
            content_class="assistant_message",
            trust_boundary="assistant",
            source_text=assistant_message,
            source_uri=None,
            metadata={"capture_mode": "turn_evidence"},
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": assistant_evidence.id,
                    "source_turn_id": source_turn_id,
                    "source_session_id": session_id,
                    "content_class": assistant_evidence.content_class,
                    "trust_boundary": assistant_evidence.trust_boundary,
                },
            }
        )
    db.add(
        MemoryEpisodeRecord(
            id=new_id_fn("mep"),
            episode_type="task_event",
            scope_key=f"session:{session_id}",
            title=_clean_text(user_message, max_chars=160),
            summary=_clean_text(user_message, max_chars=700),
            outcome=_clean_text(assistant_message, max_chars=700) if assistant_message else None,
            occurred_at=now,
            valid_from=now,
            valid_to=None,
            lifecycle_state="active",
            primary_evidence_id=user_evidence.id,
            related_entity_ids=[],
            related_assertion_ids=[],
            metadata_json={"turn_id": source_turn_id},
            created_at=now,
            updated_at=now,
        )
    )
    return events, user_evidence.id


def record_action_trace(
    db: Session,
    *,
    action_attempt: ActionAttemptRecord | None,
    scope_key: str,
    primary_evidence_id: str | None,
    source_turn_id: str | None,
    trace_type: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
    session_id: str | None = None,
    capability_id: str | None = None,
    outcome: str | None = None,
    result_refs: dict[str, Any] | None = None,
    evidence_text: str | None = None,
) -> tuple[MemoryActionTraceRecord, list[dict[str, Any]]]:
    """Create or update an action trace, ensuring its primary evidence exists.

    With an ActionAttemptRecord the outcome, capability, summary, and result refs
    are derived from the attempt exactly as the chat-turn block did; the trace is
    upserted on action_attempt_id so a later denied/expired path updates the
    chat-turn trace instead of duplicating it. Without an attempt (proactive
    execution) the caller supplies capability_id, outcome, and result_refs.

    primary_evidence_id may be None: paths without turn evidence (proactive
    execution, a denied/expired action whose turn recorded none) pass session_id
    and evidence_text, and an action-attempt evidence row is recorded so the NOT
    NULL primary_evidence_id is always satisfiable. Returns the trace and any
    evt.memory.evidence_recorded events the caller must persist.
    """
    if action_attempt is not None:
        if action_attempt.status == "succeeded":
            outcome = "succeeded"
        elif action_attempt.status == "failed":
            outcome = "failed"
        elif (
            action_attempt.status in {"rejected", "denied", "expired"}
            or action_attempt.policy_decision == "deny"
        ):
            outcome = "denied"
        else:
            outcome = "unknown"
        capability_id = action_attempt.capability_id
        summary = (
            f"{action_attempt.capability_id} {outcome} for proposal {action_attempt.proposal_index}"
        )
        result_refs = {
            "impact_level": action_attempt.impact_level,
            "policy_decision": action_attempt.policy_decision,
            "approval_required": action_attempt.approval_required,
            "execution_error": action_attempt.execution_error,
        }
        existing = db.scalar(
            select(MemoryActionTraceRecord)
            .where(MemoryActionTraceRecord.action_attempt_id == action_attempt.id)
            .with_for_update()
            .limit(1)
        )
        if existing is not None:
            existing.trace_type = trace_type
            existing.outcome = outcome
            existing.capability_id = capability_id
            existing.summary = summary
            existing.result_refs = result_refs
            existing.updated_at = now
            return existing, []
    else:
        if capability_id is None or outcome is None:
            raise MemoryEventError("record_action_trace needs capability_id and outcome")
        summary = f"{capability_id} {outcome}"
        result_refs = result_refs or {}
    events: list[dict[str, Any]] = []
    if primary_evidence_id is None:
        evidence_session_id = (
            action_attempt.session_id if action_attempt is not None else session_id
        )
        if evidence_session_id is None:
            raise MemoryEventError("record_action_trace needs session_id to record evidence")
        evidence = _record_evidence(
            db,
            session_id=evidence_session_id,
            turn_id=source_turn_id,
            actor_id="system",
            content_class="system",
            trust_boundary="system",
            source_text=evidence_text or summary,
            source_uri=None,
            metadata={"capture_mode": "action_trace_evidence"},
            now=now,
            new_id_fn=new_id_fn,
        )
        primary_evidence_id = evidence.id
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": evidence.id,
                    "source_turn_id": source_turn_id,
                    "source_session_id": evidence_session_id,
                    "content_class": evidence.content_class,
                    "trust_boundary": evidence.trust_boundary,
                },
            }
        )
    trace = MemoryActionTraceRecord(
        id=new_id_fn("mat"),
        scope_key=scope_key,
        trace_type=trace_type,
        action_attempt_id=action_attempt.id if action_attempt is not None else None,
        source_turn_id=source_turn_id,
        primary_evidence_id=primary_evidence_id,
        capability_id=capability_id,
        summary=summary,
        outcome=outcome,
        result_refs=result_refs,
        lifecycle_state="active",
        created_at=now,
        updated_at=now,
    )
    db.add(trace)
    return trace, events


def record_reasoning_trace(
    db: Session,
    *,
    scope_key: str,
    trace_type: str,
    task_summary: str,
    trace_summary: str,
    outcome: str,
    primary_evidence_id: str | None,
    source_turn_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
    session_id: str | None = None,
    related_entity_ids: list[str] | None = None,
    related_assertion_ids: list[str] | None = None,
    evidence_text: str | None = None,
) -> tuple[MemoryReasoningTraceRecord, list[dict[str, Any]]]:
    """Write a MemoryReasoningTraceRecord, ensuring its primary evidence exists.

    trace_type is one of action_path|failure|user_correction|successful_pattern|
    diagnostic; outcome is one of succeeded|failed|corrected|unknown.

    primary_evidence_id may be None: a path without turn evidence (the extraction
    worker, a turn that recorded none) passes session_id and evidence_text, and a
    reasoning-trace evidence row is recorded so the NOT NULL primary_evidence_id is
    always satisfiable. Returns the trace and any evt.memory.evidence_recorded
    events the caller must persist.
    """
    events: list[dict[str, Any]] = []
    if primary_evidence_id is None:
        if session_id is None:
            raise MemoryEventError("record_reasoning_trace needs session_id to record evidence")
        evidence = _record_evidence(
            db,
            session_id=session_id,
            turn_id=source_turn_id,
            actor_id="system",
            content_class="system",
            trust_boundary="system",
            source_text=evidence_text or trace_summary,
            source_uri=None,
            metadata={"capture_mode": "reasoning_trace_evidence"},
            now=now,
            new_id_fn=new_id_fn,
        )
        primary_evidence_id = evidence.id
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": evidence.id,
                    "source_turn_id": source_turn_id,
                    "source_session_id": session_id,
                    "content_class": evidence.content_class,
                    "trust_boundary": evidence.trust_boundary,
                },
            }
        )
    trace = MemoryReasoningTraceRecord(
        id=new_id_fn("mrt"),
        trace_type=trace_type,
        scope_key=scope_key,
        task_summary=_clean_text(task_summary, max_chars=700),
        trace_summary=_clean_text(trace_summary, max_chars=2_000),
        outcome=outcome,
        primary_evidence_id=primary_evidence_id,
        source_turn_id=source_turn_id,
        related_entity_ids=related_entity_ids or [],
        related_assertion_ids=related_assertion_ids or [],
        lifecycle_state="active",
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(trace)
    return trace, events


def propose_memory_candidate(
    db: Session,
    *,
    source_session_id: str,
    actor_id: str,
    evidence_text: str,
    subject_key: str,
    predicate: str,
    assertion_type: str,
    value: str,
    confidence: float,
    scope_key: str,
    valid_from: datetime | None,
    valid_to: datetime | None,
    extraction_model: str | None,
    extraction_prompt_version: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    source_evidence_id: str | None = None,
    proactive_case_id: str | None = None,
    settings: AppSettings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or AppSettings()
    spec = resolve_predicate_spec(predicate)
    _validate_value_kind(spec, value)
    now = now_fn()
    project_key, repo_key = scope_keys_for_policy(scope_key)
    if not resolve_memory_policy(
        db,
        operation="write",
        now=now,
        session_id=source_session_id,
        proactive_case_id=proactive_case_id,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    ).allowed:
        return []
    if (
        _never_remember_rule_for_text(
            db,
            source_session_id=source_session_id,
            scope_key=scope_key,
            text=evidence_text,
        )
        is not None
    ):
        return []
    evidence = db.get(MemoryEvidenceRecord, source_evidence_id) if source_evidence_id else None
    if evidence is not None and evidence.lifecycle_state != "available":
        return []
    evidence_was_existing = evidence is not None
    if evidence is None:
        evidence = _record_evidence(
            db,
            session_id=source_session_id,
            turn_id=None,
            actor_id=actor_id,
            content_class="system",
            trust_boundary="system" if actor_id == "system" else "trusted_user",
            source_text=evidence_text,
            source_uri=None,
            metadata={"capture_mode": "candidate_proposal"},
            now=now,
            new_id_fn=new_id_fn,
        )
    entity = _ensure_entity(
        db,
        subject_key=subject_key,
        assertion_type=assertion_type,
        now=now,
        new_id_fn=new_id_fn,
    )
    assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        subject_key=subject_key,
        predicate=predicate,
        assertion_type=assertion_type,
        value=value,
        confidence=confidence,
        scope_key=scope_key,
        is_multi_valued=spec.is_multi_valued,
        lifecycle_state="candidate",
        valid_from=valid_from,
        valid_to=valid_to,
        extraction_model=extraction_model,
        extraction_prompt_version=extraction_prompt_version,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="needs_user_review",
        actor_id="system",
        reason="candidate memory requires review",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="created",
        actor_id=actor_id,
        reason="candidate proposed",
        new_state={"lifecycle_state": assertion.lifecycle_state},
        now=now,
        new_id_fn=new_id_fn,
    )
    events = []
    if not evidence_was_existing:
        events.append(
            {
                "event_type": "evt.memory.evidence_recorded",
                "payload": {
                    "evidence_id": evidence.id,
                    "source_turn_id": None,
                    "source_session_id": source_session_id,
                    "content_class": evidence.content_class,
                    "trust_boundary": evidence.trust_boundary,
                },
            }
        )
    events.extend(
        [
            {
                "event_type": "evt.memory.candidate_proposed",
                "payload": _event_payload(assertion, evidence_id=evidence.id),
            },
            {"event_type": "evt.memory.review_required", "payload": _event_payload(assertion)},
        ]
    )
    # A conflict opens only for a "conflict"-policy predicate with a live active
    # contradiction. "supersede" predicates supersede on activation; "coexist"
    # predicates accumulate. Neither opens a conflict set.
    if spec.resolution_policy == "conflict":
        active_assertions = _active_single_assertions(
            db,
            subject_entity_id=entity.id,
            predicate=assertion.predicate,
            scope_key=assertion.scope_key,
        )
        if active_assertions:
            events.append(
                _open_conflict(
                    db,
                    assertion=assertion,
                    active_assertions=active_assertions,
                    actor_id=actor_id,
                    now=now,
                    new_id_fn=new_id_fn,
                )
            )
    events.extend(
        _maybe_enqueue_backlog_consolidation(
            db,
            scope_key=scope_key,
            settings=resolved_settings,
            now=now,
            new_id_fn=new_id_fn,
            project_key=project_key,
            repo_key=repo_key,
            actor_id=actor_id,
        )
    )
    return events


def approve_candidate(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state != "candidate":
        return []
    now = now_fn()
    events = [{"event_type": "evt.memory.candidate_approved", "payload": _event_payload(assertion)}]
    events.extend(
        _activate_assertion(
            db,
            assertion=assertion,
            actor_id=actor_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    )
    return events


def reject_candidate(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}:
        return []
    now = now_fn()
    assertion.lifecycle_state = "rejected"
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="rejected",
        actor_id=actor_id,
        reason=_clean_text(reason) if reason else "candidate rejected",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="reviewed",
        actor_id=actor_id,
        reason="candidate rejected",
        new_state={"lifecycle_state": "rejected"},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [{"event_type": "evt.memory.candidate_rejected", "payload": _event_payload(assertion)}]
    events.extend(
        _settle_conflict_sets_for_assertion(
            db, assertion_id=assertion.id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
        )
    )
    return events


def correct_assertion(
    db: Session,
    *,
    assertion_id: str,
    value: str,
    source_session_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    old_assertion = db.get(MemoryAssertionRecord, assertion_id)
    if old_assertion is None:
        return []
    if old_assertion.lifecycle_state not in {"active", "candidate", "conflicted"}:
        return []
    entity = db.get(MemoryEntityRecord, old_assertion.subject_entity_id)
    if entity is None:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": old_assertion.lifecycle_state,
        "object_value": old_assertion.object_value,
        "superseded_by_assertion_id": old_assertion.superseded_by_assertion_id,
    }
    evidence = _record_evidence(
        db,
        session_id=source_session_id,
        turn_id=None,
        actor_id=actor_id,
        content_class="system",
        trust_boundary="trusted_user",
        source_text=value,
        source_uri=None,
        metadata={"capture_mode": "manual_correction"},
        now=now,
        new_id_fn=new_id_fn,
    )
    new_assertion = _create_assertion(
        db,
        entity=entity,
        evidence=evidence,
        subject_key=old_assertion.subject_key,
        predicate=old_assertion.predicate,
        assertion_type=old_assertion.assertion_type,
        value=value,
        confidence=1.0,
        scope_key=old_assertion.scope_key,
        is_multi_valued=old_assertion.is_multi_valued,
        lifecycle_state="candidate",
        valid_from=now,
        valid_to=None,
        extraction_model=None,
        extraction_prompt_version=None,
        now=now,
        new_id_fn=new_id_fn,
    )
    old_assertion.lifecycle_state = "superseded"
    old_assertion.superseded_by_assertion_id = new_assertion.id
    old_assertion.valid_to = now
    old_assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=old_assertion.id, now=now)
    _record_version(
        db,
        table="memory_assertions",
        record_id=old_assertion.id,
        change_type="superseded",
        actor_id=actor_id,
        reason="assertion corrected",
        prior_state=prior_state,
        new_state={"superseded_by_assertion_id": new_assertion.id},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {"event_type": "evt.memory.evidence_recorded", "payload": {"evidence_id": evidence.id}},
        {"event_type": "evt.memory.assertion_superseded", "payload": _event_payload(old_assertion)},
    ]
    events.extend(
        _activate_assertion(
            db,
            assertion=new_assertion,
            actor_id=actor_id,
            now=now,
            new_id_fn=new_id_fn,
        )
    )
    events.extend(
        _settle_conflict_sets_for_assertion(
            db, assertion_id=old_assertion.id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
        )
    )
    return events


def retract_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state in {
        "deleted",
        "privacy_deleted",
        "retracted",
    }:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "retracted"
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="retract",
        actor_id=actor_id,
        reason="assertion retracted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="retracted",
        actor_id=actor_id,
        reason="assertion retracted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "retracted"},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {"event_type": "evt.memory.assertion_retracted", "payload": _event_payload(assertion)}
    ]
    events.extend(
        _settle_conflict_sets_for_assertion(
            db, assertion_id=assertion.id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
        )
    )
    return events


def delete_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state in {
        "deleted",
        "privacy_deleted",
        "retracted",
    }:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "deleted"
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="delete",
        actor_id=actor_id,
        reason="assertion deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="deleted",
        actor_id=actor_id,
        reason="assertion deleted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "deleted"},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [{"event_type": "evt.memory.assertion_deleted", "payload": _event_payload(assertion)}]
    events.extend(
        _settle_conflict_sets_for_assertion(
            db, assertion_id=assertion.id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
        )
    )
    return events


def privacy_delete_assertion(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state == "privacy_deleted":
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "privacy_deleted"
    assertion.object_value = {"text": "[privacy_deleted]"}
    assertion.valid_to = now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(
        db,
        assertion_id=assertion.id,
        now=now,
        redaction_posture="privacy_deleted",
    )
    evidence_ids = [
        evidence_id
        for evidence_id in db.scalars(
            select(MemoryAssertionEvidenceRecord.evidence_id).where(
                MemoryAssertionEvidenceRecord.assertion_id == assertion.id
            )
        ).all()
        if isinstance(evidence_id, str)
    ]
    invalidated_assertion_ids = [assertion.id]
    for version in db.scalars(
        select(MemoryVersionRecord).where(
            MemoryVersionRecord.canonical_table == "memory_assertions",
            MemoryVersionRecord.canonical_id == assertion.id,
        )
    ).all():
        for state_field in ("prior_state", "new_state"):
            state = getattr(version, state_field)
            if isinstance(state, dict) and "object_value" in state:
                setattr(
                    version, state_field, {**state, "object_value": {"text": "[privacy_deleted]"}}
                )
        version.redaction_posture = "privacy_deleted"
    for evidence_id in evidence_ids:
        evidence = db.get(MemoryEvidenceRecord, evidence_id)
        if evidence is None:
            continue
        evidence_prior_state = {
            "lifecycle_state": evidence.lifecycle_state,
            "redaction_posture": evidence.redaction_posture,
        }
        evidence.lifecycle_state = "privacy_deleted"
        evidence.source_text = "[privacy_deleted]"
        evidence.evidence_snippet = "[privacy_deleted]"
        evidence.redaction_posture = "privacy_deleted"
        evidence.updated_at = now
        _record_deletion(
            db,
            target_table="memory_evidence",
            target_id=evidence.id,
            deletion_type="privacy_delete",
            actor_id=actor_id,
            reason=reason or "evidence privacy deleted",
            redaction_posture="privacy_deleted",
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_evidence",
            record_id=evidence.id,
            change_type="privacy_deleted",
            actor_id=actor_id,
            reason=reason or "evidence privacy deleted",
            prior_state=evidence_prior_state,
            new_state={
                "lifecycle_state": "privacy_deleted",
                "redaction_posture": "privacy_deleted",
            },
            redaction_posture="privacy_deleted",
            now=now,
            new_id_fn=new_id_fn,
        )
        for linked_assertion_id in db.scalars(
            select(MemoryAssertionEvidenceRecord.assertion_id).where(
                MemoryAssertionEvidenceRecord.evidence_id == evidence.id,
                MemoryAssertionEvidenceRecord.assertion_id != assertion.id,
            )
        ).all():
            linked_assertion = db.get(MemoryAssertionRecord, linked_assertion_id)
            if linked_assertion is None or linked_assertion.lifecycle_state not in {
                "active",
                "candidate",
                "conflicted",
            }:
                continue
            linked_prior_state = {
                "lifecycle_state": linked_assertion.lifecycle_state,
                "object_value": linked_assertion.object_value,
            }
            linked_assertion.lifecycle_state = "privacy_deleted"
            linked_assertion.object_value = {"text": "[privacy_deleted]"}
            linked_assertion.valid_to = now
            linked_assertion.updated_at = now
            invalidated_assertion_ids.append(linked_assertion.id)
            linked_invalidation = _delete_projection_rows(
                db,
                assertion_id=linked_assertion.id,
                now=now,
                redaction_posture="privacy_deleted",
            )
            _record_deletion(
                db,
                target_id=linked_assertion.id,
                deletion_type="privacy_delete",
                actor_id=actor_id,
                reason=reason or "shared privacy-deleted evidence invalidated assertion",
                now=now,
                new_id_fn=new_id_fn,
                redaction_posture="privacy_deleted",
                projection_invalidation=linked_invalidation,
            )
            _record_version(
                db,
                table="memory_assertions",
                record_id=linked_assertion.id,
                change_type="privacy_deleted",
                actor_id=actor_id,
                reason=reason or "shared privacy-deleted evidence invalidated assertion",
                prior_state=linked_prior_state,
                new_state={
                    "lifecycle_state": "privacy_deleted",
                    "object_value": {"text": "[privacy_deleted]"},
                    "redaction_posture": "privacy_deleted",
                },
                redaction_posture="privacy_deleted",
                now=now,
                new_id_fn=new_id_fn,
                projection_invalidation=linked_invalidation,
            )
    _record_deletion(
        db,
        target_id=assertion.id,
        deletion_type="privacy_delete",
        actor_id=actor_id,
        reason=reason or "assertion privacy deleted",
        redaction_posture="privacy_deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="privacy_deleted",
        actor_id=actor_id,
        reason=reason or "assertion privacy deleted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "privacy_deleted", "redaction_posture": "privacy_deleted"},
        redaction_posture="privacy_deleted",
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {
            "event_type": "evt.memory.assertion_deleted",
            "payload": {
                **_event_payload(assertion),
                "deletion_type": "privacy_delete",
                "evidence_ids": evidence_ids,
            },
        }
    ]
    for invalidated_id in invalidated_assertion_ids:
        events.extend(
            _settle_conflict_sets_for_assertion(
                db, assertion_id=invalidated_id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
            )
        )
    return events


def redact_evidence(
    db: Session,
    *,
    evidence_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    evidence = db.get(MemoryEvidenceRecord, evidence_id)
    if evidence is None or evidence.lifecycle_state == "privacy_deleted":
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": evidence.lifecycle_state,
        "redaction_posture": evidence.redaction_posture,
    }
    evidence.lifecycle_state = "redacted"
    evidence.source_text = "[redacted]"
    evidence.evidence_snippet = "[redacted]"
    evidence.redaction_posture = "redacted"
    evidence.updated_at = now
    _record_deletion(
        db,
        target_table="memory_evidence",
        target_id=evidence.id,
        deletion_type="redact",
        actor_id=actor_id,
        reason=reason or "evidence redacted",
        redaction_posture="redacted",
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_evidence",
        record_id=evidence.id,
        change_type="redacted",
        actor_id=actor_id,
        reason=reason or "evidence redacted",
        prior_state=prior_state,
        new_state={"lifecycle_state": "redacted", "redaction_posture": "redacted"},
        redaction_posture="redacted",
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {"event_type": "evt.memory.evidence_redacted", "payload": {"evidence_id": evidence.id}}
    ]
    for assertion_id in db.scalars(
        select(MemoryAssertionEvidenceRecord.assertion_id).where(
            MemoryAssertionEvidenceRecord.evidence_id == evidence.id
        )
    ).all():
        assertion = db.get(MemoryAssertionRecord, assertion_id)
        if assertion is None or assertion.lifecycle_state not in {
            "active",
            "candidate",
            "conflicted",
        }:
            continue
        assertion_prior_state: dict[str, Any] = {
            "lifecycle_state": assertion.lifecycle_state,
            "object_value": assertion.object_value,
        }
        assertion.lifecycle_state = "retracted"
        assertion.object_value = {"text": "[redacted]"}
        assertion.valid_to = now
        assertion.updated_at = now
        projection_invalidation = _delete_projection_rows(
            db, assertion_id=assertion.id, now=now, redaction_posture="redacted"
        )
        _record_deletion(
            db,
            target_id=assertion.id,
            deletion_type="redact",
            actor_id=actor_id,
            reason=reason or "source evidence redacted",
            redaction_posture="redacted",
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_assertions",
            record_id=assertion.id,
            change_type="redacted",
            actor_id=actor_id,
            reason=reason or "source evidence redacted",
            prior_state=assertion_prior_state,
            new_state={
                "lifecycle_state": "retracted",
                "object_value": {"text": "[redacted]"},
                "redaction_posture": "redacted",
            },
            redaction_posture="redacted",
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {"event_type": "evt.memory.assertion_redacted", "payload": _event_payload(assertion)}
        )
    return events


def set_assertion_priority(
    db: Session,
    *,
    assertion_id: str,
    priority: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state != "active":
        return None
    now = now_fn()
    previous = db.scalar(
        select(MemorySalienceRecord)
        .where(MemorySalienceRecord.assertion_id == assertion.id)
        .limit(1)
    )
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "user_priority": previous.user_priority if previous is not None else None,
    }
    _record_salience(
        db,
        assertion=assertion,
        user_priority=priority,
        now=now,
        new_id_fn=new_id_fn,
    )
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason=f"assertion priority set to {priority}",
        prior_state=prior_state,
        new_state={"lifecycle_state": assertion.lifecycle_state, "user_priority": priority},
        now=now,
        new_id_fn=new_id_fn,
    )
    return serialize_assertion(assertion)


def set_never_remember_rule(
    db: Session,
    *,
    scope_key: str,
    pattern: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    normalized_pattern = _clean_text(pattern, max_chars=500).lower()
    if not normalized_pattern:
        return None
    now = now_fn()
    rule = db.scalar(
        select(MemoryRetentionPolicyRecord)
        .where(
            MemoryRetentionPolicyRecord.scope_key == scope_key,
            MemoryRetentionPolicyRecord.policy_kind == "never_remember",
            MemoryRetentionPolicyRecord.pattern == normalized_pattern,
        )
        .limit(1)
    )
    if rule is None:
        rule = MemoryRetentionPolicyRecord(
            id=new_id_fn("mrp"),
            scope_key=scope_key,
            policy_kind="never_remember",
            pattern=normalized_pattern,
            retention_days=None,
            lifecycle_state="active",
            reason=reason,
            metadata_json={"actor_id": actor_id},
            created_at=now,
            updated_at=now,
        )
        db.add(rule)
        db.flush()
        change_type = "created"
    else:
        rule.lifecycle_state = "active"
        rule.reason = reason
        rule.metadata_json = {"actor_id": actor_id}
        rule.updated_at = now
        change_type = "updated"
    _record_version(
        db,
        table="memory_retention_policies",
        record_id=rule.id,
        change_type=change_type,
        actor_id=actor_id,
        reason=reason or "never-remember rule set",
        new_state={
            "scope_key": rule.scope_key,
            "policy_kind": rule.policy_kind,
            "pattern": rule.pattern,
            "lifecycle_state": rule.lifecycle_state,
        },
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": rule.id,
        "scope_key": rule.scope_key,
        "policy_kind": rule.policy_kind,
        "pattern": rule.pattern,
        "state": rule.lifecycle_state,
        "reason": rule.reason,
        "created_at": to_rfc3339(rule.created_at),
        "updated_at": to_rfc3339(rule.updated_at),
    }


def set_memory_scope_binding(
    db: Session,
    *,
    scope_type: str,
    scope_key: str,
    memory_mode: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    expires_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Create or update a memory scope binding. Session mode lives on
    SessionRecord, not here, so scope_type "session" is rejected. Returns an
    evt.memory.scope_binding_changed event list ([] on rejected input)."""
    normalized_scope_type = _clean_text(scope_type, max_chars=32).lower()
    normalized_scope_key = _clean_text(scope_key, max_chars=200)
    normalized_memory_mode = _clean_text(memory_mode, max_chars=32).lower()
    if normalized_scope_type not in {"user", "project", "repo", "thread", "proactive_case"}:
        return []
    if normalized_memory_mode not in {"normal", "temporary", "no_memory"}:
        return []
    if not normalized_scope_key:
        return []
    now = now_fn()
    binding = db.scalar(
        select(MemoryScopeBindingRecord)
        .where(
            MemoryScopeBindingRecord.scope_type == normalized_scope_type,
            MemoryScopeBindingRecord.scope_key == normalized_scope_key,
            MemoryScopeBindingRecord.actor_id == actor_id,
        )
        .limit(1)
    )
    enabled = normalized_memory_mode == "normal"
    metadata = {"source": "memory_scope_mode"}
    if binding is None:
        binding = MemoryScopeBindingRecord(
            id=new_id_fn("msb"),
            scope_type=normalized_scope_type,
            scope_key=normalized_scope_key,
            actor_id=actor_id,
            memory_mode=normalized_memory_mode,
            extraction_enabled=enabled,
            recall_enabled=enabled,
            reason=reason or "memory scope mode updated",
            expires_at=expires_at,
            metadata_json=metadata,
            created_at=now,
            updated_at=now,
        )
        db.add(binding)
        db.flush()
        change_type = "created"
    else:
        binding.memory_mode = normalized_memory_mode
        binding.extraction_enabled = enabled
        binding.recall_enabled = enabled
        binding.reason = reason or "memory scope mode updated"
        binding.expires_at = expires_at
        binding.metadata_json = metadata
        binding.updated_at = now
        change_type = "updated"
    binding_state = {
        "id": binding.id,
        "scope_type": binding.scope_type,
        "scope_key": binding.scope_key,
        "actor_id": binding.actor_id,
        "memory_mode": binding.memory_mode,
        "extraction_enabled": binding.extraction_enabled,
        "recall_enabled": binding.recall_enabled,
        "reason": binding.reason,
        "expires_at": to_rfc3339(binding.expires_at) if binding.expires_at else None,
        "created_at": to_rfc3339(binding.created_at),
        "updated_at": to_rfc3339(binding.updated_at),
    }
    _record_version(
        db,
        table="memory_scope_bindings",
        record_id=binding.id,
        change_type=change_type,
        actor_id=actor_id,
        reason=binding.reason,
        new_state=binding_state,
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.scope_binding_changed", "payload": binding_state}]


def resolve_conflict(
    db: Session,
    *,
    conflict_set_id: str,
    assertion_id: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    conflict = db.get(MemoryConflictSetRecord, conflict_set_id)
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if conflict is None or assertion is None or conflict.lifecycle_state != "open":
        return []
    member_ids = [
        row.assertion_id
        for row in db.scalars(
            select(MemoryConflictMemberRecord)
            .where(MemoryConflictMemberRecord.conflict_set_id == conflict.id)
            .order_by(
                MemoryConflictMemberRecord.created_at.asc(), MemoryConflictMemberRecord.id.asc()
            )
        ).all()
    ]
    if assertion.id not in member_ids or assertion.lifecycle_state not in {"active", "conflicted"}:
        return []
    now = now_fn()
    conflict.lifecycle_state = "resolved"
    conflict.resolution_assertion_id = assertion.id
    conflict.updated_at = now
    events = _activate_assertion(
        db,
        assertion=assertion,
        actor_id=actor_id,
        now=now,
        new_id_fn=new_id_fn,
    )
    for losing_id in member_ids:
        if losing_id == assertion.id:
            continue
        losing_assertion = db.get(MemoryAssertionRecord, losing_id)
        if losing_assertion is None:
            continue
        # A previously-active loser is superseded by the winner so its history is
        # preserved; a candidate/conflicted loser is rejected. Already-terminal
        # members (superseded, retracted, ...) are left untouched.
        if losing_assertion.lifecycle_state == "active":
            losing_assertion.lifecycle_state = "superseded"
            losing_assertion.superseded_by_assertion_id = assertion.id
            losing_assertion.valid_to = losing_assertion.valid_to or now
            losing_assertion.updated_at = now
            projection_invalidation = _delete_projection_rows(
                db, assertion_id=losing_assertion.id, now=now
            )
            _record_version(
                db,
                table="memory_assertions",
                record_id=losing_assertion.id,
                change_type="superseded",
                actor_id=actor_id,
                reason="conflict loser superseded by resolution winner",
                new_state={
                    "lifecycle_state": "superseded",
                    "superseded_by_assertion_id": assertion.id,
                },
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
            )
            events.append(
                {
                    "event_type": "evt.memory.assertion_superseded",
                    "payload": _event_payload(losing_assertion),
                }
            )
        elif losing_assertion.lifecycle_state in {"candidate", "conflicted"}:
            losing_assertion.lifecycle_state = "rejected"
            losing_assertion.valid_to = losing_assertion.valid_to or now
            losing_assertion.updated_at = now
            projection_invalidation = _delete_projection_rows(
                db, assertion_id=losing_assertion.id, now=now
            )
            _record_review(
                db,
                assertion_id=losing_assertion.id,
                decision="rejected",
                actor_id=actor_id,
                reason="conflict resolved in favor of another assertion",
                now=now,
                new_id_fn=new_id_fn,
            )
            _record_version(
                db,
                table="memory_assertions",
                record_id=losing_assertion.id,
                change_type="reviewed",
                actor_id=actor_id,
                reason="conflict loser rejected",
                new_state={"lifecycle_state": "rejected"},
                projection_invalidation=projection_invalidation,
                now=now,
                new_id_fn=new_id_fn,
            )
            events.append(
                {
                    "event_type": "evt.memory.candidate_rejected",
                    "payload": _event_payload(losing_assertion),
                }
            )
    events.append(
        {
            "event_type": "evt.memory.conflict_resolved",
            "payload": {
                "conflict_set_id": conflict.id,
                "resolution_assertion_id": assertion.id,
            },
        }
    )
    return events


def edit_candidate(
    db: Session,
    *,
    assertion_id: str,
    value: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "object_value": assertion.object_value,
    }
    assertion.object_value = {"text": _clean_text(value)}
    assertion.updated_at = now
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate edited",
        prior_state=prior_state,
        new_state={
            "lifecycle_state": assertion.lifecycle_state,
            "object_value": assertion.object_value,
        },
        now=now,
        new_id_fn=new_id_fn,
    )
    return [{"event_type": "evt.memory.candidate_edited", "payload": _event_payload(assertion)}]


def merge_candidates(
    db: Session,
    *,
    assertion_ids: Sequence[str],
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    ids = [assertion_id for assertion_id in assertion_ids if assertion_id]
    if len(ids) < 2:
        return []
    assertions = [db.get(MemoryAssertionRecord, assertion_id) for assertion_id in ids]
    if any(
        assertion is None or assertion.lifecycle_state not in {"candidate", "conflicted"}
        for assertion in assertions
    ):
        return []
    now = now_fn()
    winner = assertions[0]
    assert winner is not None
    merged_values = [
        _assertion_text(assertion)
        for assertion in assertions
        if assertion is not None and _assertion_text(assertion)
    ]
    prior_state = {"lifecycle_state": winner.lifecycle_state, "object_value": winner.object_value}
    winner.object_value = {"text": _clean_text("; ".join(dict.fromkeys(merged_values)))}
    winner.lifecycle_state = "candidate"
    winner.updated_at = now
    events = [{"event_type": "evt.memory.candidates_merged", "payload": _event_payload(winner)}]
    _record_version(
        db,
        table="memory_assertions",
        record_id=winner.id,
        change_type="updated",
        actor_id=actor_id,
        reason="candidate merge winner updated",
        prior_state=prior_state,
        new_state={"lifecycle_state": winner.lifecycle_state, "object_value": winner.object_value},
        now=now,
        new_id_fn=new_id_fn,
    )
    for loser in assertions[1:]:
        assert loser is not None
        loser_prior_state = {"lifecycle_state": loser.lifecycle_state}
        loser.lifecycle_state = "rejected"
        loser.valid_to = now
        loser.updated_at = now
        projection_invalidation = _delete_projection_rows(db, assertion_id=loser.id, now=now)
        _record_review(
            db,
            assertion_id=loser.id,
            decision="rejected",
            actor_id=actor_id,
            reason=f"merged into {winner.id}",
            now=now,
            new_id_fn=new_id_fn,
        )
        _record_version(
            db,
            table="memory_assertions",
            record_id=loser.id,
            change_type="reviewed",
            actor_id=actor_id,
            reason=f"candidate merged into {winner.id}",
            prior_state=loser_prior_state,
            new_state={"lifecycle_state": "rejected", "merged_into_assertion_id": winner.id},
            projection_invalidation=projection_invalidation,
            now=now,
            new_id_fn=new_id_fn,
        )
        events.append(
            {"event_type": "evt.memory.candidate_rejected", "payload": _event_payload(loser)}
        )
    return events


def mark_assertion_stale(
    db: Session,
    *,
    assertion_id: str,
    actor_id: str,
    reason: str | None,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    # A stale assertion must identify its staleness rationale; a missing or blank
    # reason is a typed error, normalized at this boundary before any mutation.
    staleness_reason = _clean_text(reason) if reason else ""
    if not staleness_reason:
        raise MemoryStaleReasonRequiredError("marking an assertion stale requires a reason")
    assertion = db.get(MemoryAssertionRecord, assertion_id)
    if assertion is None or assertion.lifecycle_state not in {"active", "candidate", "conflicted"}:
        return []
    now = now_fn()
    prior_state = {
        "lifecycle_state": assertion.lifecycle_state,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
    }
    assertion.lifecycle_state = "stale"
    assertion.valid_to = assertion.valid_to or now
    assertion.updated_at = now
    projection_invalidation = _delete_projection_rows(db, assertion_id=assertion.id, now=now)
    _record_review(
        db,
        assertion_id=assertion.id,
        decision="needs_operator_review",
        actor_id=actor_id,
        reason=staleness_reason,
        now=now,
        new_id_fn=new_id_fn,
    )
    _record_version(
        db,
        table="memory_assertions",
        record_id=assertion.id,
        change_type="updated",
        actor_id=actor_id,
        reason=staleness_reason,
        prior_state=prior_state,
        new_state={"lifecycle_state": "stale", "staleness_reason": staleness_reason},
        projection_invalidation=projection_invalidation,
        now=now,
        new_id_fn=new_id_fn,
    )
    events = [
        {"event_type": "evt.memory.assertion_marked_stale", "payload": _event_payload(assertion)}
    ]
    events.extend(
        _settle_conflict_sets_for_assertion(
            db, assertion_id=assertion.id, actor_id=actor_id, now=now, new_id_fn=new_id_fn
        )
    )
    return events


def create_relationship(
    db: Session,
    *,
    source_entity_id: str,
    target_entity_id: str,
    relationship_type: str,
    evidence_id: str,
    scope_key: str,
    confidence: float,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> dict[str, Any] | None:
    source = db.get(MemoryEntityRecord, source_entity_id)
    target = db.get(MemoryEntityRecord, target_entity_id)
    evidence = db.get(MemoryEvidenceRecord, evidence_id)
    if (
        source is None
        or target is None
        or evidence is None
        or evidence.lifecycle_state != "available"
    ):
        return None
    now = now_fn()
    relationship = MemoryRelationshipRecord(
        id=new_id_fn("mrl"),
        source_entity_id=source.id,
        target_entity_id=target.id,
        relationship_type=_memory_key(relationship_type),
        scope_key=scope_key,
        lifecycle_state="active",
        confidence=max(0.0, min(confidence, 1.0)),
        valid_from=now,
        valid_to=None,
        evidence_id=evidence.id,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(relationship)
    db.flush()
    # The graph projection is rebuilt asynchronously: a graph job recomputes the
    # BFS reachability of this relationship's source entity to depth 3.
    db.add(
        MemoryProjectionJobRecord(
            id=new_id_fn("mpj"),
            projection_kind="graph",
            target_table="memory_entities",
            target_id=source.id,
            lifecycle_state="pending",
            attempts=0,
            max_retries=3,
            error=None,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )
    _record_version(
        db,
        table="memory_relationships",
        record_id=relationship.id,
        change_type="created",
        actor_id=actor_id,
        reason="relationship created",
        new_state={"lifecycle_state": "active"},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "relationship_id": relationship.id,
        "source_entity_id": source.id,
        "target_entity_id": target.id,
        "relationship_type": relationship.relationship_type,
    }


_CONSOLIDATION_JOB_KINDS = ("context_block", "project_state", "hot_index", "topic_block")


def enqueue_consolidation_job(
    db: Session,
    *,
    scope_key: str,
    kind: str,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> MemoryProjectionJobRecord | None:
    """Insert a consolidation MemoryProjectionJobRecord for a scope. Returns None
    when a pending job of the same kind already targets the scope, so repeated
    autonomous triggers do not pile up duplicate work."""
    if kind not in _CONSOLIDATION_JOB_KINDS:
        raise MemoryProjectionError(f"unsupported consolidation job kind: {kind}")
    existing = db.scalar(
        select(MemoryProjectionJobRecord)
        .where(
            MemoryProjectionJobRecord.projection_kind == kind,
            MemoryProjectionJobRecord.target_table == "memory_scopes",
            MemoryProjectionJobRecord.target_id == scope_key,
            MemoryProjectionJobRecord.lifecycle_state == "pending",
        )
        .limit(1)
    )
    if existing is not None:
        return None
    job = MemoryProjectionJobRecord(
        id=new_id_fn("mpj"),
        projection_kind=kind,
        target_table="memory_scopes",
        target_id=scope_key,
        lifecycle_state="pending",
        attempts=0,
        max_retries=3,
        error=None,
        run_after=now,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return job


def _maybe_enqueue_backlog_consolidation(
    db: Session,
    *,
    scope_key: str,
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
    project_key: str | None,
    repo_key: str | None,
    actor_id: str | None,
) -> list[dict[str, Any]]:
    """Enqueue a hot_index consolidation job when the candidate or open-conflict
    backlog for a scope crosses its threshold. Gated by consolidate policy."""
    candidate_backlog = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .where(
                MemoryAssertionRecord.scope_key == scope_key,
                MemoryAssertionRecord.lifecycle_state == "candidate",
            )
        )
        or 0
    )
    conflict_backlog = (
        db.scalar(
            select(func.count())
            .select_from(MemoryConflictSetRecord)
            .where(
                MemoryConflictSetRecord.scope_key == scope_key,
                MemoryConflictSetRecord.lifecycle_state == "open",
            )
        )
        or 0
    )
    candidate_crossed = candidate_backlog >= settings.memory_consolidation_candidate_threshold
    conflict_crossed = conflict_backlog >= settings.memory_consolidation_conflict_threshold
    if not (candidate_crossed or conflict_crossed):
        return []
    if not resolve_memory_policy(
        db,
        operation="consolidate",
        now=now,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    ).allowed:
        return []
    job = enqueue_consolidation_job(
        db, scope_key=scope_key, kind="hot_index", now=now, new_id_fn=new_id_fn
    )
    if job is None:
        return []
    return [
        {
            "event_type": "evt.memory.consolidation_enqueued",
            "payload": {
                "scope_key": scope_key,
                "projection_job_id": job.id,
                "trigger": "candidate_backlog" if candidate_crossed else "conflict_backlog",
                "candidate_backlog": candidate_backlog,
                "conflict_backlog": conflict_backlog,
            },
        }
    ]


def consolidate_memory(
    db: Session,
    *,
    scope_key: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    source_session_id: str | None = None,
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or AppSettings()
    budget_tokens = resolved_settings.memory_hot_index_budget_tokens
    hard_max_tokens = resolved_settings.memory_hot_index_hard_max_tokens
    now = now_fn()
    project_key, repo_key = scope_keys_for_policy(scope_key)
    policy = resolve_memory_policy(
        db,
        operation="consolidate",
        now=now,
        session_id=source_session_id,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    )
    if not policy.allowed:
        return {
            "scope_key": scope_key,
            "status": "skipped",
            "reason": policy.reason,
            "memory_policy": policy.as_dict(),
            "selected_source_ids": [],
            "omitted_sources": [],
            "proposed_changes": [],
            "applied_projection_changes": [],
            "rejected_changes": [],
        }

    assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(MemoryAssertionRecord.lifecycle_state == "active")
        .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
    ).all()
    if scope_key != "global":
        assertions = [
            assertion
            for assertion in assertions
            if assertion.scope_key == scope_key or assertion.subject_key == scope_key
        ]
    selected_assertions = assertions[:24]
    source_assertion_ids = [assertion.id for assertion in selected_assertions]
    source_versions = {
        "memory_assertions": {
            assertion.id: _memory_version_number(
                db, table="memory_assertions", record_id=assertion.id
            )
            for assertion in selected_assertions
        }
    }
    omitted_sources = [
        {"id": assertion.id, "kind": "semantic_assertion", "reason": "outside consolidation budget"}
        for assertion in assertions[24:]
    ]
    proposed_changes: list[dict[str, Any]] = []
    applied_projection_changes: list[dict[str, Any]] = []
    rejected_changes: list[dict[str, Any]] = []

    duplicates: dict[tuple[str, str, str], list[MemoryAssertionRecord]] = {}
    for assertion in selected_assertions:
        if assertion.is_multi_valued:
            continue
        duplicates.setdefault(
            (assertion.subject_key, assertion.predicate, assertion.scope_key), []
        ).append(assertion)
    for duplicate_group in duplicates.values():
        if len(duplicate_group) < 2:
            continue
        for duplicate in duplicate_group[1:]:
            _record_review(
                db,
                assertion_id=duplicate.id,
                decision="needs_operator_review",
                actor_id="system",
                reason="consolidation found duplicate single-valued active memory",
                now=now,
                new_id_fn=new_id_fn,
            )
            proposed_changes.append(
                {
                    "kind": "merge_or_supersede",
                    "assertion_id": duplicate.id,
                    "winner_assertion_id": duplicate_group[0].id,
                    "review_state": "needs_operator_review",
                }
            )

    for assertion in selected_assertions:
        if assertion.valid_to is not None and assertion.valid_to <= now:
            _record_review(
                db,
                assertion_id=assertion.id,
                decision="needs_operator_review",
                actor_id="system",
                reason="consolidation found expired active memory",
                now=now,
                new_id_fn=new_id_fn,
            )
            proposed_changes.append(
                {
                    "kind": "mark_stale",
                    "assertion_id": assertion.id,
                    "review_state": "needs_operator_review",
                }
            )

    topic_family_by_type = {
        "profile": "user-profile",
        "preference": "user-preferences",
        "project_state": "active-projects",
        "decision": "architecture-decisions",
        "commitment": "commitments",
        "procedure": "procedures",
    }
    for assertion_type, family in topic_family_by_type.items():
        family_assertions = [
            assertion
            for assertion in selected_assertions
            if assertion.assertion_type == assertion_type
        ]
        if not family_assertions:
            continue
        topic_key = f"{scope_key}:{family}"
        topic = db.scalar(
            select(MemoryTopicRecord)
            .where(
                MemoryTopicRecord.scope_key == scope_key, MemoryTopicRecord.topic_key == topic_key
            )
            .limit(1)
        )
        summary = "; ".join(_assertion_text(assertion) for assertion in family_assertions[:6])
        if topic is None:
            topic = MemoryTopicRecord(
                id=new_id_fn("mtp"),
                topic_key=topic_key,
                family=family,
                scope_key=scope_key,
                title=family.replace("-", " "),
                summary=_clean_text(summary, max_chars=700),
                lifecycle_state="active",
                projection_version=MEMORY_PROJECTION_VERSION,
                metadata_json={"source": "consolidation"},
                created_at=now,
                updated_at=now,
            )
            db.add(topic)
            db.flush()
        else:
            topic.family = family
            topic.summary = _clean_text(summary, max_chars=700)
            topic.lifecycle_state = "active"
            topic.projection_version = MEMORY_PROJECTION_VERSION
            topic.updated_at = now
        rank = 0
        for assertion in family_assertions[:12]:
            member = db.scalar(
                select(MemoryTopicMemberRecord)
                .where(
                    MemoryTopicMemberRecord.topic_id == topic.id,
                    MemoryTopicMemberRecord.canonical_table == "memory_assertions",
                    MemoryTopicMemberRecord.canonical_id == assertion.id,
                )
                .limit(1)
            )
            if member is None:
                db.add(
                    MemoryTopicMemberRecord(
                        id=new_id_fn("mtm"),
                        topic_id=topic.id,
                        canonical_table="memory_assertions",
                        canonical_id=assertion.id,
                        membership_kind="source",
                        rank=rank,
                        metadata_json={"source": "consolidation"},
                        created_at=now,
                    )
                )
            else:
                member.rank = rank
                member.metadata_json = {"source": "consolidation"}
            rank += 1
        # A topic context block must point at its topic; the schema CHECK also
        # enforces this, but a typed defect here is clearer than an IntegrityError.
        if not topic.id:
            raise MemoryProjectionError(
                f"topic context block for family {family} in scope {scope_key} has no topic_id"
            )
        topic_block = db.scalar(
            select(MemoryContextBlockRecord)
            .where(
                MemoryContextBlockRecord.block_type == "topic",
                MemoryContextBlockRecord.scope_key == scope_key,
                MemoryContextBlockRecord.topic_id == topic.id,
                MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
            )
            .limit(1)
        )
        if topic_block is None:
            db.add(
                MemoryContextBlockRecord(
                    id=new_id_fn("mcb"),
                    block_type="topic",
                    scope_key=scope_key,
                    content=topic.summary,
                    topic_id=topic.id,
                    lifecycle_state="active",
                    source_assertion_ids=[assertion.id for assertion in family_assertions[:12]],
                    source_episode_ids=[],
                    source_trace_ids=[],
                    source_action_trace_ids=[],
                    source_procedure_ids=[],
                    source_project_state_snapshot_ids=[],
                    source_memory_versions=source_versions,
                    source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
                    projection_version=MEMORY_PROJECTION_VERSION,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            topic_block.content = topic.summary
            topic_block.lifecycle_state = "active"
            topic_block.source_assertion_ids = [
                assertion.id for assertion in family_assertions[:12]
            ]
            topic_block.source_memory_versions = source_versions
            topic_block.source_projection_versions = {
                "memory_context_blocks": MEMORY_PROJECTION_VERSION
            }
            topic_block.updated_at = now
        applied_projection_changes.append(
            {
                "kind": "topic_block",
                "topic_id": topic.id,
                "family": family,
                "source_assertion_ids": [assertion.id for assertion in family_assertions[:12]],
            }
        )

    action_traces = db.scalars(
        select(MemoryActionTraceRecord)
        .where(
            MemoryActionTraceRecord.lifecycle_state == "active",
            MemoryActionTraceRecord.outcome == "succeeded",
        )
        .order_by(MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc())
        .limit(50)
    ).all()
    if scope_key != "global":
        action_traces = [
            trace
            for trace in action_traces
            if trace.scope_key == scope_key or trace.scope_key == f"session:{scope_key}"
        ]
    traces_by_capability: dict[str, list[MemoryActionTraceRecord]] = {}
    for trace in action_traces:
        if trace.capability_id is None:
            continue
        traces_by_capability.setdefault(trace.capability_id, []).append(trace)
    for capability_id, traces in traces_by_capability.items():
        if len(traces) < 2:
            continue
        procedure_key = _memory_key(f"successful {capability_id}")
        procedure = db.scalar(
            select(MemoryProcedureRecord)
            .where(
                MemoryProcedureRecord.procedure_key == procedure_key,
                MemoryProcedureRecord.scope_key == scope_key,
            )
            .limit(1)
        )
        if procedure is not None:
            rejected_changes.append(
                {
                    "kind": "procedure_candidate",
                    "capability_id": capability_id,
                    "reason": "procedure already exists",
                }
            )
            continue
        db.add(
            MemoryProcedureRecord(
                id=new_id_fn("mpr"),
                procedure_key=procedure_key,
                scope_key=scope_key,
                title=f"Successful {capability_id}",
                instruction=(
                    "Review repeated successful action traces before converting this "
                    "candidate into durable procedure memory."
                ),
                lifecycle_state="candidate",
                review_state="needs_operator_review",
                source_assertion_id=None,
                primary_evidence_id=traces[0].primary_evidence_id,
                valid_from=now,
                valid_to=None,
                metadata_json={
                    "source": "consolidation",
                    "capability_id": capability_id,
                    "source_action_trace_ids": [trace.id for trace in traces[:5]],
                },
                created_at=now,
                updated_at=now,
            )
        )
        proposed_changes.append(
            {
                "kind": "procedure_candidate",
                "capability_id": capability_id,
                "source_action_trace_ids": [trace.id for trace in traces[:5]],
                "review_state": "needs_operator_review",
            }
        )

    reasoning_traces = db.scalars(
        select(MemoryReasoningTraceRecord)
        .where(MemoryReasoningTraceRecord.lifecycle_state == "active")
        .order_by(MemoryReasoningTraceRecord.updated_at.desc(), MemoryReasoningTraceRecord.id.asc())
        .limit(50)
    ).all()
    if scope_key != "global":
        reasoning_traces = [
            reasoning_trace
            for reasoning_trace in reasoning_traces
            if reasoning_trace.scope_key == scope_key
            or reasoning_trace.scope_key == f"session:{scope_key}"
        ]
    successful_by_task: dict[str, list[MemoryReasoningTraceRecord]] = {}
    for reasoning_trace in reasoning_traces:
        if reasoning_trace.trace_type == "successful_pattern":
            successful_by_task.setdefault(_memory_key(reasoning_trace.task_summary), []).append(
                reasoning_trace
            )
    for task_key, task_traces in successful_by_task.items():
        if len(task_traces) < 2:
            continue
        procedure_key = _memory_key(f"reasoning {task_key}")
        procedure = db.scalar(
            select(MemoryProcedureRecord)
            .where(
                MemoryProcedureRecord.procedure_key == procedure_key,
                MemoryProcedureRecord.scope_key == scope_key,
            )
            .limit(1)
        )
        if procedure is not None:
            rejected_changes.append(
                {
                    "kind": "procedure_candidate",
                    "task_key": task_key,
                    "reason": "procedure already exists",
                }
            )
            continue
        db.add(
            MemoryProcedureRecord(
                id=new_id_fn("mpr"),
                procedure_key=procedure_key,
                scope_key=scope_key,
                title=_clean_text(f"Pattern: {task_traces[0].task_summary}", max_chars=200),
                instruction=(
                    "Review repeated successful reasoning traces before converting this "
                    "candidate into durable procedure memory."
                ),
                lifecycle_state="candidate",
                review_state="needs_operator_review",
                source_assertion_id=None,
                primary_evidence_id=task_traces[0].primary_evidence_id,
                valid_from=now,
                valid_to=None,
                metadata_json={
                    "source": "consolidation",
                    "task_key": task_key,
                    "source_reasoning_trace_ids": [t.id for t in task_traces[:5]],
                },
                created_at=now,
                updated_at=now,
            )
        )
        proposed_changes.append(
            {
                "kind": "procedure_candidate",
                "task_key": task_key,
                "source_reasoning_trace_ids": [t.id for t in task_traces[:5]],
                "review_state": "needs_operator_review",
            }
        )

    candidate_events: list[dict[str, Any]] = []
    negative_predicate_by_trace_type = {
        "failure": "negative.known_bad_path",
        "user_correction": "negative.invalid_assumption",
    }
    for reasoning_trace in reasoning_traces:
        predicate = negative_predicate_by_trace_type.get(reasoning_trace.trace_type)
        if predicate is None:
            continue
        trace_evidence = db.get(MemoryEvidenceRecord, reasoning_trace.primary_evidence_id)
        if trace_evidence is None or trace_evidence.lifecycle_state != "available":
            continue
        events = propose_memory_candidate(
            db,
            source_session_id=trace_evidence.source_session_id,
            actor_id="system",
            evidence_text=reasoning_trace.trace_summary,
            subject_key=scope_key,
            predicate=predicate,
            assertion_type="negative",
            value=_clean_text(reasoning_trace.trace_summary, max_chars=700),
            confidence=0.6,
            scope_key=scope_key,
            valid_from=None,
            valid_to=None,
            extraction_model=None,
            extraction_prompt_version=None,
            now_fn=now_fn,
            new_id_fn=new_id_fn,
            source_evidence_id=reasoning_trace.primary_evidence_id,
        )
        candidate_events.extend(events)
        for event in events:
            event_payload = event.get("payload")
            if event.get("event_type") == "evt.memory.candidate_proposed" and isinstance(
                event_payload, dict
            ):
                proposed_changes.append(
                    {
                        "kind": "negative_memory_candidate",
                        "predicate": predicate,
                        "assertion_id": event_payload.get("assertion_id"),
                        "source_reasoning_trace_id": reasoning_trace.id,
                    }
                )

    # Hot index: a salience-ranked set of pointer entries plus the WS-5 "do not
    # repeat" section. Entries carry source ids only, never verbatim values, so
    # the block stays a compact index. The rebuild is held inside the token
    # budget by evicting the lowest-salience entries; the "do not repeat" section
    # is policy-mandated and is not evicted.
    salience_by_id = {
        row.assertion_id: row.score
        for row in db.scalars(
            select(MemorySalienceRecord).where(
                MemorySalienceRecord.assertion_id.in_(source_assertion_ids or [""])
            )
        ).all()
    }
    hot_entries: list[dict[str, Any]] = [
        {
            "predicate": assertion.predicate,
            "scope_key": assertion.scope_key,
            "salience_score": salience_by_id.get(assertion.id, 0.0),
            "source_assertion_ids": [assertion.id],
        }
        for assertion in selected_assertions
        if assertion.assertion_type != "negative"
    ]
    # Highest salience first; updated_at then id break ties deterministically so
    # eviction always drops the same lowest-priority entry for a given state.
    salience_rank_by_id = {
        assertion.id: (
            -salience_by_id.get(assertion.id, 0.0),
            assertion.updated_at,
            assertion.id,
        )
        for assertion in selected_assertions
    }
    hot_entries.sort(key=lambda entry: salience_rank_by_id[entry["source_assertion_ids"][0]])
    negative_assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(
            MemoryAssertionRecord.lifecycle_state == "active",
            MemoryAssertionRecord.assertion_type == "negative",
        )
        .order_by(MemoryAssertionRecord.updated_at.desc(), MemoryAssertionRecord.id.asc())
        .limit(24)
    ).all()
    if scope_key != "global":
        negative_assertions = [
            assertion
            for assertion in negative_assertions
            if assertion.scope_key == scope_key or assertion.subject_key == scope_key
        ]
    do_not_repeat: list[dict[str, Any]] = [
        {"predicate": assertion.predicate, "source_assertion_ids": [assertion.id]}
        for assertion in negative_assertions
    ]

    def _render_hot_index(entries: list[dict[str, Any]]) -> str:
        return json.dumps(
            {
                "scope_key": scope_key,
                "generated_at": to_rfc3339(now),
                "entry_count": len(entries),
                "entries": entries,
                "do_not_repeat": do_not_repeat,
            },
            sort_keys=True,
        )

    content = _render_hot_index(hot_entries)
    while hot_entries and count_context_tokens(content) > budget_tokens:
        hot_entries.pop()
        content = _render_hot_index(hot_entries)
    hot_index_tokens = count_context_tokens(content)
    if hot_index_tokens > hard_max_tokens:
        raise MemoryProjectionError(
            f"rebuilt hot index for scope {scope_key} is {hot_index_tokens} tokens, "
            f"over the {hard_max_tokens}-token hard max"
        )
    hot_index_source_ids = [entry["source_assertion_ids"][0] for entry in hot_entries] + [
        entry["source_assertion_ids"][0] for entry in do_not_repeat
    ]
    block = db.scalar(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type == "hot_index",
            MemoryContextBlockRecord.scope_key == scope_key,
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .limit(1)
    )
    if block is None:
        block = MemoryContextBlockRecord(
            id=new_id_fn("mcb"),
            block_type="hot_index",
            scope_key=scope_key,
            content=content,
            topic_id=None,
            lifecycle_state="active",
            source_assertion_ids=hot_index_source_ids,
            source_episode_ids=[],
            source_trace_ids=[],
            source_action_trace_ids=[],
            source_procedure_ids=[],
            source_project_state_snapshot_ids=[],
            source_memory_versions=source_versions,
            source_projection_versions={"memory_context_blocks": MEMORY_PROJECTION_VERSION},
            projection_version=MEMORY_PROJECTION_VERSION,
            created_at=now,
            updated_at=now,
        )
        db.add(block)
    else:
        block.content = content
        block.lifecycle_state = "active"
        block.source_assertion_ids = hot_index_source_ids
        block.source_memory_versions = source_versions
        block.source_projection_versions = {"memory_context_blocks": MEMORY_PROJECTION_VERSION}
        block.updated_at = now
    emit_memory_events(
        db,
        events=[
            *candidate_events,
            {
                "event_type": "evt.memory.consolidation_completed",
                "payload": {
                    "scope_key": scope_key,
                    "context_block_id": block.id,
                    "selected_source_ids": source_assertion_ids,
                    "proposed_change_count": len(proposed_changes),
                    "applied_projection_count": len(applied_projection_changes) + 1,
                },
            },
        ],
        entry_path="consolidation",
        actor_id=actor_id,
        scope_key=scope_key,
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "scope_key": scope_key,
        "status": "completed",
        "context_block_id": block.id,
        "selected_source_ids": source_assertion_ids,
        "omitted_sources": omitted_sources,
        "proposed_changes": proposed_changes,
        "applied_projection_changes": [
            *applied_projection_changes,
            {
                "kind": "hot_index",
                "context_block_id": block.id,
                "source_assertion_ids": source_assertion_ids,
            },
        ],
        "rejected_changes": rejected_changes,
        "updated_at": to_rfc3339(block.updated_at),
    }


def export_memory(
    db: Session,
    *,
    scope_key: str,
    actor_id: str,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    source_session_id: str | None = None,
) -> dict[str, Any]:
    now = now_fn()
    project_key, repo_key = scope_keys_for_policy(scope_key)
    policy = resolve_memory_policy(
        db,
        operation="recall",
        now=now,
        session_id=source_session_id,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    )
    if not policy.allowed:
        return {
            "id": None,
            "scope_key": scope_key,
            "export_format": "json",
            "status": "skipped",
            "redaction_posture": "redacted",
            "source_counts": {},
            "content": {},
            "memory_policy": policy.as_dict(),
            "created_at": to_rfc3339(now),
        }
    payload = list_memory(db)
    if scope_key != "global":
        for key in ("active_assertions", "candidates"):
            payload[key] = [
                item
                for item in payload[key]
                if item["scope_key"] == scope_key or item["subject_key"] == scope_key
            ]
        payload["conflicts"] = [
            item for item in payload["conflicts"] if item["scope_key"] == scope_key
        ]
        payload["project_state"] = [
            item
            for item in payload["project_state"]
            if f"project:{item['project_key']}" == scope_key
        ]
        for key in (
            "procedures",
            "action_traces",
            "reasoning_traces",
            "topics",
            "context_blocks",
            "scope_bindings",
        ):
            payload[key] = [item for item in payload[key] if item["scope_key"] == scope_key]
        allowed_evidence_ids = {
            ref["evidence_id"]
            for key in ("active_assertions", "candidates")
            for item in payload[key]
            for ref in item.get("evidence_refs", [])
            if isinstance(ref, dict) and isinstance(ref.get("evidence_id"), str)
        }
        allowed_evidence_ids.update(
            evidence_id
            for item in payload["project_state"]
            for evidence_id in item.get("source_evidence_ids", [])
            if isinstance(evidence_id, str)
        )
        allowed_evidence_ids.update(
            item["primary_evidence_id"]
            for key in ("action_traces", "reasoning_traces")
            for item in payload[key]
            if isinstance(item.get("primary_evidence_id"), str)
        )
        payload["evidence"] = [
            item for item in payload["evidence"] if item["id"] in allowed_evidence_ids
        ]
        payload["deletions"] = []
        payload["retention_policies"] = [
            item for item in payload["retention_policies"] if item["scope_key"] == scope_key
        ]
        payload["sensitivity_labels"] = []
        payload["export_artifacts"] = []
        payload["eval_runs"] = []
    artifact = MemoryExportArtifactRecord(
        id=new_id_fn("mea"),
        scope_key=scope_key,
        export_format="json",
        status="created",
        projection_version=MEMORY_PROJECTION_VERSION,
        redaction_posture="redacted",
        content=payload,
        source_counts={
            "active_assertions": len(payload["active_assertions"]),
            "project_state": len(payload["project_state"]),
            "procedures": len(payload["procedures"]),
        },
        source_memory_versions={
            "memory_assertions": {
                item["id"]: _memory_version_number(
                    db, table="memory_assertions", record_id=item["id"]
                )
                for item in payload["active_assertions"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            },
            "memory_context_blocks": {
                item["id"]: _memory_version_number(
                    db, table="memory_context_blocks", record_id=item["id"]
                )
                for item in payload["context_blocks"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            },
        },
        source_projection_versions={
            "memory_export_artifacts": MEMORY_PROJECTION_VERSION,
            "memory_context_blocks": MEMORY_PROJECTION_VERSION,
        },
        actor_id=actor_id,
        created_at=now,
        updated_at=now,
    )
    db.add(artifact)
    db.flush()
    _record_version(
        db,
        table="memory_export_artifacts",
        record_id=artifact.id,
        change_type="exported",
        actor_id=actor_id,
        reason="memory exported",
        new_state={"scope_key": artifact.scope_key, "source_counts": artifact.source_counts},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": artifact.id,
        "scope_key": artifact.scope_key,
        "export_format": artifact.export_format,
        "status": artifact.status,
        "projection_version": artifact.projection_version,
        "redaction_posture": artifact.redaction_posture,
        "source_counts": artifact.source_counts,
        "source_memory_versions": artifact.source_memory_versions,
        "source_projection_versions": artifact.source_projection_versions,
        "content": artifact.content,
        "created_at": to_rfc3339(artifact.created_at),
    }


def import_memory_candidates(
    db: Session,
    *,
    source_session_id: str,
    actor_id: str,
    candidates: Sequence[dict[str, Any]],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    cutover_enabled: bool = False,
) -> list[str]:
    if not cutover_enabled:
        return []
    imported_ids: list[str] = []
    for item in candidates[:50]:
        evidence_text = item.get("evidence_text")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        assertion_type = item.get("assertion_type")
        value = item.get("value")
        confidence = item.get("confidence")
        scope_key = item.get("scope_key")
        if (
            not isinstance(evidence_text, str)
            or not evidence_text.strip()
            or not isinstance(subject_key, str)
            or not subject_key.strip()
            or not isinstance(predicate, str)
            or not predicate.strip()
            or assertion_type not in ALLOWED_MEMORY_ASSERTION_TYPES
            or not isinstance(value, str)
            or not value.strip()
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or float(confidence) < 0.0
            or float(confidence) > 1.0
            or float(confidence) != float(confidence)
            or not isinstance(scope_key, str)
            or not scope_key.strip()
        ):
            continue
        evidence_text = evidence_text.strip()
        subject_key = subject_key.strip()
        predicate = predicate.strip()
        value = value.strip()
        scope_key = scope_key.strip()
        existing = db.scalar(
            select(MemoryAssertionRecord)
            .where(
                MemoryAssertionRecord.subject_key == subject_key,
                MemoryAssertionRecord.predicate == predicate,
                MemoryAssertionRecord.assertion_type == assertion_type,
                MemoryAssertionRecord.scope_key == scope_key,
                MemoryAssertionRecord.object_value == {"text": _clean_text(value)},
                MemoryAssertionRecord.lifecycle_state.in_(("candidate", "conflicted", "active")),
            )
            .order_by(MemoryAssertionRecord.created_at.asc(), MemoryAssertionRecord.id.asc())
            .limit(1)
        )
        if existing is not None:
            imported_ids.append(existing.id)
            continue
        events = propose_memory_candidate(
            db,
            source_session_id=source_session_id,
            actor_id=actor_id,
            evidence_text=evidence_text,
            subject_key=subject_key,
            predicate=predicate,
            assertion_type=assertion_type,
            value=value,
            confidence=float(confidence),
            scope_key=scope_key,
            valid_from=None,
            valid_to=None,
            extraction_model=None,
            extraction_prompt_version="memory-import-v1",
            now_fn=now_fn,
            new_id_fn=new_id_fn,
        )
        for event in events:
            payload = event.get("payload")
            if (
                event.get("event_type") == "evt.memory.candidate_proposed"
                and isinstance(payload, dict)
                and isinstance(payload.get("assertion_id"), str)
            ):
                imported_ids.append(payload["assertion_id"])
    return imported_ids


def run_memory_eval(
    db: Session,
    *,
    eval_name: str,
    cases: Sequence[dict[str, Any]],
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    now = now_fn()
    case_results: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    retrieval_latency_ms = 0.0
    context_tokens = 0
    expected_total = 0
    expected_recalled = 0
    selected_labeled = 0
    selected_relevant = 0
    omitted_relevant = 0
    conflict_case_total = 0
    conflict_case_handled = 0
    for index, raw_case in enumerate(cases[:100], start=1):
        query = raw_case.get("query")
        if not isinstance(query, str) or not query.strip():
            case_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "reason": "case missing query",
                    "selected_memory_ids": [],
                    "omitted_memory_count": 0,
                }
            )
            failed_count += 1
            continue
        expected_ids = [
            item for item in raw_case.get("expected_memory_ids", []) if isinstance(item, str)
        ]
        forbidden_ids = [
            item for item in raw_case.get("forbidden_memory_ids", []) if isinstance(item, str)
        ]
        expected_kinds = [
            item for item in raw_case.get("expected_kinds", []) if isinstance(item, str)
        ]
        expected_text = raw_case.get("expected")
        forbidden_texts = [
            item.lower()
            for item in raw_case.get("forbidden_texts", [])
            if isinstance(item, str) and item
        ]
        expect_conflict = bool(raw_case.get("expect_conflict"))
        raw_max = raw_case.get("max_recalled_assertions")
        max_selected = raw_max if isinstance(raw_max, int) and raw_max > 0 else 8
        started = time.perf_counter()
        try:
            memory_context, recall_event = build_memory_context(
                db,
                user_message=query,
                max_recalled_assertions=max_selected,
                settings=settings,
                current_session_id=current_session_id,
            )
        except AIJudgmentFailure as exc:
            retrieval_latency_ms += (time.perf_counter() - started) * 1000.0
            case_results.append(
                {
                    "index": index,
                    "status": "failed",
                    "reason": exc.safe_reason,
                    "selected_memory_ids": [],
                    "omitted_memory_count": 0,
                    "parse_status": exc.parse_status,
                    "validation_status": exc.validation_status,
                }
            )
            failed_count += 1
            continue

        retrieval_latency_ms += (time.perf_counter() - started) * 1000.0
        recall_window = memory_context["recall_window"]
        selected_memory_ids = [
            item for item in recall_window["selected_memory_ids"] if isinstance(item, str)
        ]
        candidate_memory_ids = [
            item for item in recall_window["candidate_memory_ids"] if isinstance(item, str)
        ]
        omitted_memory_ids = {
            item["id"]
            for item in recall_window["omitted_memories"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        selected_kinds = [
            item["kind"]
            for item in recall_window["selected_memories"]
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        ]
        selected_text = json.dumps(
            {
                "hot_index": memory_context["hot_index"],
                "topic_index": memory_context["topic_index"],
                "semantic_assertions": memory_context["semantic_assertions"],
                "project_state": memory_context["project_state"],
                "procedural_memory": memory_context["procedural_memory"],
                "action_traces": memory_context["action_traces"],
            },
            sort_keys=True,
        ).lower()
        context_tokens += count_context_tokens(context_text(memory_context))
        failures: list[str] = []
        for expected_id in expected_ids:
            if expected_id not in selected_memory_ids:
                failures.append(f"missing expected memory {expected_id}")
        for forbidden_id in forbidden_ids:
            if forbidden_id in selected_memory_ids:
                failures.append(f"selected forbidden memory {forbidden_id}")
        for expected_kind in expected_kinds:
            if expected_kind not in selected_kinds:
                failures.append(f"missing expected kind {expected_kind}")
        if isinstance(expected_text, str) and expected_text.strip():
            expected_terms = set(_terms(expected_text))
            selected_terms = set(_terms(selected_text))
            if expected_terms and not expected_terms.intersection(selected_terms):
                failures.append("missing expected text signal")
        for forbidden_text in forbidden_texts:
            if forbidden_text in selected_text:
                failures.append("selected forbidden text")
        memory_policy = memory_context.get("memory_policy")
        if bool(raw_case.get("expect_policy_blocked")) and (
            not isinstance(memory_policy, dict) or memory_policy.get("allowed") is not False
        ):
            failures.append("expected memory policy block")
        if expect_conflict and not memory_context["conflicts"]:
            failures.append("expected an open conflict to be surfaced")

        # Metric accounting. A case's expected ids are its relevant set; recall
        # is over the candidate pool; precision is over the selected memories the
        # case explicitly labelled (expected or forbidden), so unlabelled
        # selections in a case that only asserts a conflict do not skew it.
        labelled_ids = set(expected_ids) | set(forbidden_ids)
        expected_total += len(expected_ids)
        expected_recalled += sum(1 for eid in expected_ids if eid in candidate_memory_ids)
        selected_labeled += sum(1 for sid in selected_memory_ids if sid in labelled_ids)
        selected_relevant += sum(1 for sid in selected_memory_ids if sid in expected_ids)
        omitted_relevant += sum(1 for eid in expected_ids if eid in omitted_memory_ids)
        if expect_conflict:
            conflict_case_total += 1
            if memory_context["conflicts"]:
                conflict_case_handled += 1

        if failures:
            status = "failed"
            failed_count += 1
        else:
            status = "passed"
            passed_count += 1
        case_results.append(
            {
                "index": index,
                "status": status,
                "query": _clean_text(query),
                "failures": failures,
                "selected_memory_ids": selected_memory_ids,
                "selected_memory_kinds": selected_kinds,
                "candidate_memory_ids": candidate_memory_ids,
                "omitted_memory_count": recall_window["omitted_memory_count"],
                "memory_candidate_count": recall_window["memory_candidate_count"],
                "curation_confidence": recall_window["curation_confidence"],
                "memory_policy": memory_policy,
                "projection_health": memory_context["projection_health"],
                "recall_diagnostics": recall_event,
            }
        )
    active_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .where(MemoryAssertionRecord.lifecycle_state == "active")
        )
        or 0
    )
    candidate_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .where(MemoryAssertionRecord.lifecycle_state.in_(("candidate", "conflicted")))
        )
        or 0
    )
    conflict_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryConflictSetRecord)
            .where(MemoryConflictSetRecord.lifecycle_state == "open")
        )
        or 0
    )
    failed_projection_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryProjectionJobRecord)
            .where(MemoryProjectionJobRecord.lifecycle_state.in_(("failed", "dead_letter")))
        )
        or 0
    )
    pass_rate = passed_count / len(case_results) if case_results else 1.0
    run = MemoryEvalRunRecord(
        id=new_id_fn("mer"),
        eval_name=_clean_text(eval_name, max_chars=200) or "memory eval",
        status="completed" if failed_count == 0 else "failed",
        metrics={
            "active_assertions": int(active_count),
            "pending_candidates": int(candidate_count),
            "open_conflicts": int(conflict_count),
            "failed_projection_jobs": int(failed_projection_count),
            "case_count": len(cases),
            "passed_cases": passed_count,
            "failed_cases": failed_count,
            "pass_rate": pass_rate,
            # The memory.md eval metric set. The eval exercises recall only, so
            # retrieval latency is measured and the extraction/curation/projection/
            # consolidation stages it does not run are recorded as zero.
            "answer_accuracy": pass_rate,
            "candidate_recall": expected_recalled / expected_total if expected_total else 1.0,
            "curation_precision": (
                selected_relevant / selected_labeled if selected_labeled else 1.0
            ),
            "selected_relevant_memory_count": selected_relevant,
            "omitted_relevant_memory_count": omitted_relevant,
            "conflict_handling_accuracy": (
                conflict_case_handled / conflict_case_total if conflict_case_total else 1.0
            ),
            "context_tokens": context_tokens,
            "extraction_latency_ms": 0.0,
            "retrieval_latency_ms": retrieval_latency_ms,
            "curation_latency_ms": 0.0,
            "projection_latency_ms": 0.0,
            "consolidation_latency_ms": 0.0,
        },
        cases=case_results,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.flush()
    _record_version(
        db,
        table="memory_eval_runs",
        record_id=run.id,
        change_type="created",
        actor_id="system",
        reason="memory eval completed",
        new_state={"metrics": run.metrics},
        now=now,
        new_id_fn=new_id_fn,
    )
    return {
        "id": run.id,
        "eval_name": run.eval_name,
        "status": run.status,
        "metrics": run.metrics,
        "cases": run.cases,
        "created_at": to_rfc3339(run.created_at),
        "updated_at": to_rfc3339(run.updated_at),
    }


def retry_projection_job(
    db: Session,
    *,
    job_id: str,
    now_fn: Callable[[], datetime],
) -> dict[str, Any] | None:
    job = db.get(MemoryProjectionJobRecord, job_id)
    if job is None:
        return None
    now = now_fn()
    job.lifecycle_state = "pending"
    job.error = None
    job.run_after = now
    job.updated_at = now
    return {
        "id": job.id,
        "projection_kind": job.projection_kind,
        "target_table": job.target_table,
        "target_id": job.target_id,
        "state": job.lifecycle_state,
        "run_after": to_rfc3339(job.run_after),
    }


def record_rotation_context_block(
    db: Session,
    *,
    rotation_id: str,
    prior_session_id: str,
    new_session_id: str,
    rotation_reason: str,
    prior_turns: Sequence[TurnRecord],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    now = now_fn()
    source_turns = [
        {
            "turn_id": turn.id,
            "user_message": _clean_text(turn.user_message, max_chars=900),
            "assistant_message": _clean_text(turn.assistant_message or "", max_chars=900),
            "status": turn.status,
            "created_at": to_rfc3339(turn.created_at),
        }
        for turn in prior_turns
    ]
    source_turn_ids = [turn["turn_id"] for turn in source_turns]
    input_refs = {
        "rotation_id": rotation_id,
        "rotation_reason": rotation_reason,
        "prior_session_id": prior_session_id,
        "new_session_id": new_session_id,
        "source_turn_ids": source_turn_ids,
    }
    ai_judgment_id = new_id_fn("ajg")
    if source_turns:
        try:
            payload = _curate_rotation_context_with_model(
                rotation_reason=rotation_reason,
                prior_session_id=prior_session_id,
                new_session_id=new_session_id,
                source_turns=source_turns,
                settings=settings,
            )
        except AIJudgmentFailure as exc:
            db.add(
                AIJudgmentRecord(
                    id=ai_judgment_id,
                    judgment_type="continuity_compaction",
                    source_type="session_rotation",
                    source_id=rotation_id,
                    status="failed",
                    model=settings.model_name,
                    prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
                    provider_response_id=exc.provider_response_id,
                    input_summary="session-rotation continuity compaction",
                    input_refs=input_refs,
                    selected=[],
                    omitted=[],
                    output={},
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status=exc.parse_status,
                    validation_status=exc.validation_status,
                    failure_code=exc.code,
                    failure_reason=exc.safe_reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            raise
        except Exception as exc:
            failure = AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason=f"continuity curation failed: {exc.__class__.__name__}",
                retryable=True,
                parse_status="missing_output",
                validation_status="not_validated",
            )
            db.add(
                AIJudgmentRecord(
                    id=ai_judgment_id,
                    judgment_type="continuity_compaction",
                    source_type="session_rotation",
                    source_id=rotation_id,
                    status="failed",
                    model=settings.model_name,
                    prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
                    provider_response_id=None,
                    input_summary="session-rotation continuity compaction",
                    input_refs=input_refs,
                    selected=[],
                    omitted=[],
                    output={},
                    rationale=None,
                    uncertainty=None,
                    confidence=None,
                    parse_status=failure.parse_status,
                    validation_status=failure.validation_status,
                    failure_code=failure.code,
                    failure_reason=failure.safe_reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
            raise failure from exc
        summary = _clean_text(str(payload["summary"]), max_chars=2000)
        decisions = payload.get("decisions")
        open_loops = payload.get("open_loops")
        unresolved_uncertainty = payload.get("unresolved_uncertainty")
        important_omissions = payload.get("important_omissions")
        preserved_turn_refs = payload.get("preserved_turn_refs")
        omitted_turn_refs = payload.get("omitted_turn_refs")
        user_commitments = payload.get("user_commitments")
        assistant_commitments = payload.get("assistant_commitments")
        tool_action_outcomes = payload.get("tool_action_outcomes")
        confidence = payload.get("confidence")
        model = payload.get("model")
        parse_status = payload.get("parse_status")
        validation_status = payload.get("validation_status")
        provider_response_id = payload.get("provider_response_id")
    else:
        summary = "No source turns required continuity curation."
        decisions = []
        open_loops = []
        unresolved_uncertainty = []
        important_omissions = []
        preserved_turn_refs = []
        omitted_turn_refs = []
        user_commitments = []
        assistant_commitments = []
        tool_action_outcomes = []
        confidence = 1.0
        model = None
        parse_status = "not_required_no_candidates"
        validation_status = "not_validated"
        provider_response_id = None
    db.add(
        AIJudgmentRecord(
            id=ai_judgment_id,
            judgment_type="continuity_compaction",
            source_type="session_rotation",
            source_id=rotation_id,
            status="succeeded",
            model=model
            if isinstance(model, str)
            else settings.model_name
            if source_turns
            else None,
            prompt_version=MEMORY_CONTINUITY_PROMPT_VERSION,
            provider_response_id=provider_response_id
            if isinstance(provider_response_id, str)
            else None,
            input_summary="session-rotation continuity compaction",
            input_refs=input_refs,
            selected=preserved_turn_refs if isinstance(preserved_turn_refs, list) else [],
            omitted=omitted_turn_refs if isinstance(omitted_turn_refs, list) else [],
            output={
                "continuity_compaction": {
                    "summary": summary,
                    "source_turn_ids": source_turn_ids,
                    "preserved_turn_refs": preserved_turn_refs
                    if isinstance(preserved_turn_refs, list)
                    else [],
                    "omitted_turn_refs": omitted_turn_refs
                    if isinstance(omitted_turn_refs, list)
                    else [],
                    "provider_response_id": provider_response_id
                    if isinstance(provider_response_id, str)
                    else None,
                    "decisions": decisions if isinstance(decisions, list) else [],
                    "open_loops": open_loops if isinstance(open_loops, list) else [],
                    "unresolved_uncertainty": unresolved_uncertainty
                    if isinstance(unresolved_uncertainty, list)
                    else [],
                    "important_omissions": important_omissions
                    if isinstance(important_omissions, list)
                    else [],
                }
            },
            rationale=summary if source_turns else None,
            uncertainty=None,
            confidence=max(0.0, min(float(confidence), 1.0))
            if isinstance(confidence, int | float)
            else None,
            parse_status=parse_status if isinstance(parse_status, str) else "parsed",
            validation_status="valid" if source_turns else "not_validated",
            failure_code=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ProjectStateSnapshotRecord(
            id=new_id_fn("pss"),
            project_key="session_continuity",
            summary=summary,
            state={
                "ai_judgment_id": ai_judgment_id,
                "rotation_id": rotation_id,
                "rotation_reason": rotation_reason,
                "prior_session_id": prior_session_id,
                "new_session_id": new_session_id,
                "provider_response_id": provider_response_id
                if isinstance(provider_response_id, str)
                else None,
                "source_turn_ids": source_turn_ids,
                "preserved_turn_refs": preserved_turn_refs
                if isinstance(preserved_turn_refs, list)
                else [],
                "omitted_turn_refs": omitted_turn_refs
                if isinstance(omitted_turn_refs, list)
                else [],
                "user_commitments": user_commitments if isinstance(user_commitments, list) else [],
                "assistant_commitments": assistant_commitments
                if isinstance(assistant_commitments, list)
                else [],
                "decisions": decisions if isinstance(decisions, list) else [],
                "open_loops": open_loops if isinstance(open_loops, list) else [],
                "unresolved_uncertainty": unresolved_uncertainty
                if isinstance(unresolved_uncertainty, list)
                else [],
                "tool_action_outcomes": tool_action_outcomes
                if isinstance(tool_action_outcomes, list)
                else [],
                "important_omissions": important_omissions
                if isinstance(important_omissions, list)
                else [],
                "confidence": max(0.0, min(float(confidence), 1.0))
                if isinstance(confidence, int | float)
                else None,
                "model": model
                if isinstance(model, str)
                else settings.model_name
                if source_turns
                else None,
                "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
                "parse_status": parse_status if isinstance(parse_status, str) else "parsed",
                "validation_status": validation_status
                if isinstance(validation_status, str)
                else "valid",
            },
            source_assertion_ids=[],
            source_episode_ids=[],
            source_evidence_ids=[],
            lifecycle_state="active",
            projection_version=MEMORY_PROJECTION_VERSION,
            created_at=now,
            updated_at=now,
        )
    )


def _curate_rotation_context_with_model(
    *,
    rotation_reason: str,
    prior_session_id: str,
    new_session_id: str,
    source_turns: Sequence[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    if settings.openai_api_key is None:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_CREDENTIALS",
            safe_reason="AI continuity curation requires ARIEL_OPENAI_API_KEY",
            retryable=False,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.model_name,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You are Ariel's continuity curator. Summarize the closed session for "
                            "future turns. Return JSON only with summary, preserved_turn_refs, "
                            "omitted_turn_refs, user_commitments, assistant_commitments, decisions, "
                            "open_loops, tool_action_outcomes, unresolved_uncertainty, "
                            "important_omissions, and confidence. Every source turn must appear "
                            "exactly once in preserved_turn_refs or omitted_turn_refs with a reason. "
                            "Do not answer the user."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
                                "rotation_reason": rotation_reason,
                                "prior_session_id": prior_session_id,
                                "new_session_id": new_session_id,
                                "source_turns": list(source_turns),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                "store": False,
                "text": {"verbosity": "low"},
            },
            timeout=settings.model_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_TIMEOUT",
            safe_reason="continuity curation model timed out",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    except httpx.HTTPError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="continuity curation model network request failed",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    if response.status_code >= 400:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason=f"continuity curation model returned HTTP {response.status_code}",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="continuity curation provider returned invalid JSON",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    provider_response_id = response_payload.get("id")
    provider_response_id = provider_response_id if isinstance(provider_response_id, str) else None
    try:
        payload = json.loads(_extract_output_text(response_payload.get("output")))
    except json.JSONDecodeError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_INVALID_JSON",
            safe_reason="continuity curation model returned malformed JSON",
            retryable=False,
            parse_status="invalid_json",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        ) from exc
    return validate_continuity_compaction_payload(
        payload,
        source_turn_ids=[
            turn["turn_id"]
            for turn in source_turns
            if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)
        ],
        model=settings.model_name,
        provider_response_id=provider_response_id,
    )


def _evidence_refs_by_assertion(
    db: Session,
    assertion_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    if not assertion_ids:
        return {}
    rows = db.execute(
        select(MemoryAssertionEvidenceRecord.assertion_id, MemoryEvidenceRecord)
        .join(
            MemoryEvidenceRecord,
            MemoryEvidenceRecord.id == MemoryAssertionEvidenceRecord.evidence_id,
        )
        .where(MemoryAssertionEvidenceRecord.assertion_id.in_(assertion_ids))
        .order_by(
            MemoryAssertionEvidenceRecord.assertion_id.asc(),
            MemoryAssertionEvidenceRecord.created_at.asc(),
            MemoryAssertionEvidenceRecord.id.asc(),
        )
    ).all()
    result: dict[str, list[dict[str, Any]]] = {assertion_id: [] for assertion_id in assertion_ids}
    for assertion_id, evidence in rows:
        result.setdefault(assertion_id, []).append(
            {
                "evidence_id": evidence.id,
                "snippet": evidence.evidence_snippet
                or redact_text(_clean_text(evidence.source_text)),
                "source_turn_id": evidence.source_turn_id,
                "source_session_id": evidence.source_session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
                "created_at": to_rfc3339(evidence.created_at),
            }
        )
    return result


def serialize_assertion(
    assertion: MemoryAssertionRecord,
    *,
    evidence_refs: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "id": assertion.id,
        "subject_key": assertion.subject_key,
        "predicate": assertion.predicate,
        "type": assertion.assertion_type,
        "state": assertion.lifecycle_state,
        "value": redact_text(_assertion_text(assertion)),
        "confidence": assertion.confidence,
        "scope_key": assertion.scope_key,
        "is_multi_valued": assertion.is_multi_valued,
        "valid_from": to_rfc3339(assertion.valid_from) if assertion.valid_from else None,
        "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
        "last_verified_at": to_rfc3339(assertion.last_verified_at),
        "created_at": to_rfc3339(assertion.created_at),
        "updated_at": to_rfc3339(assertion.updated_at),
        "superseded_by_id": assertion.superseded_by_assertion_id,
        "evidence_refs": list(evidence_refs),
        "projection_version": MEMORY_PROJECTION_VERSION,
    }


def validate_continuity_compaction_payload(
    raw_payload: Any,
    *,
    source_turn_ids: Sequence[str],
    model: str | None,
    provider_response_id: str | None,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="continuity compaction model returned a non-object JSON value",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    required_lists = (
        "preserved_turn_refs",
        "omitted_turn_refs",
        "user_commitments",
        "assistant_commitments",
        "decisions",
        "open_loops",
        "tool_action_outcomes",
        "unresolved_uncertainty",
        "important_omissions",
    )
    summary = raw_payload.get("summary")
    confidence = raw_payload.get("confidence")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(confidence, int | float)
        or any(not isinstance(raw_payload.get(key), list) for key in required_lists)
    ):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="continuity compaction JSON missing required fields",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )
    if float(confidence) < 0.0 or float(confidence) > 1.0:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="continuity compaction confidence must be between 0 and 1",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    known_turn_ids = set(source_turn_ids)
    seen_turn_ids: set[str] = set()
    preserved_turn_refs: list[dict[str, str]] = []
    omitted_turn_refs: list[dict[str, str]] = []
    for key, target in (
        ("preserved_turn_refs", preserved_turn_refs),
        ("omitted_turn_refs", omitted_turn_refs),
    ):
        for ref in raw_payload[key]:
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("turn_id"), str)
                or not isinstance(ref.get("reason"), str)
                or not ref["reason"].strip()
            ):
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_SCHEMA",
                    safe_reason=f"continuity compaction {key} entries need turn_id and reason",
                    retryable=False,
                    parse_status="schema_invalid",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            turn_id = ref["turn_id"]
            if turn_id not in known_turn_ids:
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_VALIDATION",
                    safe_reason="continuity compaction referenced an unknown source turn",
                    retryable=False,
                    parse_status="parsed",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            if turn_id in seen_turn_ids:
                raise AIJudgmentFailure(
                    code="E_AI_JUDGMENT_VALIDATION",
                    safe_reason="continuity compaction duplicated a source turn reference",
                    retryable=False,
                    parse_status="parsed",
                    validation_status="invalid",
                    provider_response_id=provider_response_id,
                )
            seen_turn_ids.add(turn_id)
            target.append({"turn_id": turn_id, "reason": _clean_text(ref["reason"], max_chars=500)})

    if seen_turn_ids != known_turn_ids:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="continuity compaction must account for every source turn",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        )

    payload = {
        "summary": _clean_text(summary, max_chars=2000),
        "source_turn_ids": list(source_turn_ids),
        "preserved_turn_refs": preserved_turn_refs,
        "omitted_turn_refs": omitted_turn_refs,
        "user_commitments": list(raw_payload["user_commitments"]),
        "assistant_commitments": list(raw_payload["assistant_commitments"]),
        "decisions": list(raw_payload["decisions"]),
        "open_loops": list(raw_payload["open_loops"]),
        "tool_action_outcomes": list(raw_payload["tool_action_outcomes"]),
        "unresolved_uncertainty": list(raw_payload["unresolved_uncertainty"]),
        "important_omissions": list(raw_payload["important_omissions"]),
        "confidence": float(confidence),
        "model": model,
        "prompt_version": MEMORY_CONTINUITY_PROMPT_VERSION,
        "provider_response_id": provider_response_id,
        "parse_status": "parsed",
        "validation_status": "valid",
    }
    return payload


def _serialize_conflict(conflict: MemoryConflictSetRecord) -> dict[str, Any]:
    return {
        "id": conflict.id,
        "subject_entity_id": conflict.subject_entity_id,
        "predicate": conflict.predicate,
        "scope_key": conflict.scope_key,
        "state": conflict.lifecycle_state,
        "resolution_assertion_id": conflict.resolution_assertion_id,
        "reason": conflict.reason,
        "created_at": to_rfc3339(conflict.created_at),
        "updated_at": to_rfc3339(conflict.updated_at),
    }


def list_memory(db: Session) -> dict[str, Any]:
    assertions = db.scalars(
        select(MemoryAssertionRecord).order_by(
            MemoryAssertionRecord.updated_at.desc(),
            MemoryAssertionRecord.id.asc(),
        )
    ).all()
    evidence_refs = _evidence_refs_by_assertion(db, [assertion.id for assertion in assertions])
    conflicts = db.scalars(
        select(MemoryConflictSetRecord).order_by(
            MemoryConflictSetRecord.updated_at.desc(),
            MemoryConflictSetRecord.id.asc(),
        )
    ).all()
    evidence_rows = db.scalars(
        select(MemoryEvidenceRecord)
        .order_by(MemoryEvidenceRecord.created_at.desc(), MemoryEvidenceRecord.id.desc())
        .limit(50)
    ).all()
    procedures = db.scalars(
        select(MemoryProcedureRecord)
        .where(MemoryProcedureRecord.lifecycle_state == "active")
        .order_by(MemoryProcedureRecord.updated_at.desc(), MemoryProcedureRecord.id.asc())
        .limit(50)
    ).all()
    project_state = db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.lifecycle_state == "active")
        .order_by(
            ProjectStateSnapshotRecord.updated_at.desc(),
            ProjectStateSnapshotRecord.id.desc(),
        )
        .limit(50)
    ).all()
    action_traces = db.scalars(
        select(MemoryActionTraceRecord)
        .where(MemoryActionTraceRecord.lifecycle_state == "active")
        .order_by(MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc())
        .limit(50)
    ).all()
    reasoning_traces = db.scalars(
        select(MemoryReasoningTraceRecord)
        .where(MemoryReasoningTraceRecord.lifecycle_state == "active")
        .order_by(MemoryReasoningTraceRecord.updated_at.desc(), MemoryReasoningTraceRecord.id.asc())
        .limit(50)
    ).all()
    topics = db.scalars(
        select(MemoryTopicRecord)
        .where(MemoryTopicRecord.lifecycle_state == "active")
        .order_by(MemoryTopicRecord.updated_at.desc(), MemoryTopicRecord.id.asc())
        .limit(50)
    ).all()
    context_blocks = db.scalars(
        select(MemoryContextBlockRecord)
        .where(MemoryContextBlockRecord.lifecycle_state == "active")
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.asc())
        .limit(50)
    ).all()
    deletions = db.scalars(
        select(MemoryDeletionRecord)
        .order_by(MemoryDeletionRecord.created_at.desc(), MemoryDeletionRecord.id.desc())
        .limit(50)
    ).all()
    scope_bindings = db.scalars(
        select(MemoryScopeBindingRecord)
        .order_by(MemoryScopeBindingRecord.updated_at.desc(), MemoryScopeBindingRecord.id.asc())
        .limit(50)
    ).all()
    retention_policies = db.scalars(
        select(MemoryRetentionPolicyRecord)
        .order_by(
            MemoryRetentionPolicyRecord.updated_at.desc(), MemoryRetentionPolicyRecord.id.asc()
        )
        .limit(50)
    ).all()
    sensitivity_labels = db.scalars(
        select(MemorySensitivityLabelRecord)
        .where(MemorySensitivityLabelRecord.lifecycle_state == "active")
        .order_by(
            MemorySensitivityLabelRecord.updated_at.desc(), MemorySensitivityLabelRecord.id.asc()
        )
        .limit(50)
    ).all()
    export_artifacts = db.scalars(
        select(MemoryExportArtifactRecord)
        .order_by(
            MemoryExportArtifactRecord.created_at.desc(), MemoryExportArtifactRecord.id.desc()
        )
        .limit(20)
    ).all()
    eval_runs = db.scalars(
        select(MemoryEvalRunRecord)
        .order_by(MemoryEvalRunRecord.created_at.desc(), MemoryEvalRunRecord.id.desc())
        .limit(20)
    ).all()
    return {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "active_assertions": [
            serialize_assertion(assertion, evidence_refs=evidence_refs.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state == "active"
        ],
        "candidates": [
            serialize_assertion(assertion, evidence_refs=evidence_refs.get(assertion.id, []))
            for assertion in assertions
            if assertion.lifecycle_state in {"candidate", "conflicted"}
        ],
        "conflicts": [_serialize_conflict(conflict) for conflict in conflicts],
        "project_state": [
            {
                "id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": _redact_json_value(snapshot.state),
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
                "created_at": to_rfc3339(snapshot.created_at),
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
            for snapshot in project_state
        ],
        "evidence": [
            {
                "id": evidence.id,
                "source_turn_id": evidence.source_turn_id,
                "source_session_id": evidence.source_session_id,
                "content_class": evidence.content_class,
                "trust_boundary": evidence.trust_boundary,
                "state": evidence.lifecycle_state,
                "snippet": evidence.evidence_snippet
                or redact_text(_clean_text(evidence.source_text)),
                "created_at": to_rfc3339(evidence.created_at),
            }
            for evidence in evidence_rows
        ],
        "procedures": [
            {
                "id": procedure.id,
                "procedure_key": procedure.procedure_key,
                "scope_key": procedure.scope_key,
                "title": procedure.title,
                "instruction": redact_text(procedure.instruction),
                "state": procedure.lifecycle_state,
                "review_state": procedure.review_state,
                "source_assertion_id": procedure.source_assertion_id,
                "created_at": to_rfc3339(procedure.created_at),
                "updated_at": to_rfc3339(procedure.updated_at),
            }
            for procedure in procedures
        ],
        "action_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "action_attempt_id": trace.action_attempt_id,
                "source_turn_id": trace.source_turn_id,
                "capability_id": trace.capability_id,
                "summary": redact_text(trace.summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "result_refs": trace.result_refs,
                "created_at": to_rfc3339(trace.created_at),
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in action_traces
        ],
        "reasoning_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "task_summary": redact_text(trace.task_summary),
                "trace_summary": redact_text(trace.trace_summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "source_turn_id": trace.source_turn_id,
                "related_entity_ids": trace.related_entity_ids,
                "related_assertion_ids": trace.related_assertion_ids,
                "created_at": to_rfc3339(trace.created_at),
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in reasoning_traces
        ],
        "topics": [
            {
                "id": topic.id,
                "topic_key": topic.topic_key,
                "family": topic.family,
                "scope_key": topic.scope_key,
                "title": topic.title,
                "summary": redact_text(topic.summary),
                "state": topic.lifecycle_state,
                "projection_version": topic.projection_version,
                "created_at": to_rfc3339(topic.created_at),
                "updated_at": to_rfc3339(topic.updated_at),
            }
            for topic in topics
        ],
        "context_blocks": [
            {
                "id": block.id,
                "block_type": block.block_type,
                "scope_key": block.scope_key,
                "topic_id": block.topic_id,
                "content": redact_text(block.content),
                "state": block.lifecycle_state,
                "source_assertion_ids": block.source_assertion_ids,
                "source_episode_ids": block.source_episode_ids,
                "source_trace_ids": block.source_trace_ids,
                "source_action_trace_ids": block.source_action_trace_ids,
                "source_procedure_ids": block.source_procedure_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "source_memory_versions": block.source_memory_versions,
                "source_projection_versions": block.source_projection_versions,
                "projection_version": block.projection_version,
                "created_at": to_rfc3339(block.created_at),
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in context_blocks
        ],
        "deletions": [
            {
                "id": deletion.id,
                "target_table": deletion.target_table,
                "target_id": deletion.target_id,
                "deletion_type": deletion.deletion_type,
                "actor_id": deletion.actor_id,
                "reason": deletion.reason,
                "redaction_posture": deletion.redaction_posture,
                "projection_invalidation": deletion.projection_invalidation,
                "created_at": to_rfc3339(deletion.created_at),
            }
            for deletion in deletions
        ],
        "scope_bindings": [
            {
                "id": binding.id,
                "scope_type": binding.scope_type,
                "scope_key": binding.scope_key,
                "actor_id": binding.actor_id,
                "memory_mode": binding.memory_mode,
                "extraction_enabled": binding.extraction_enabled,
                "recall_enabled": binding.recall_enabled,
                "reason": binding.reason,
                "expires_at": to_rfc3339(binding.expires_at) if binding.expires_at else None,
                "created_at": to_rfc3339(binding.created_at),
                "updated_at": to_rfc3339(binding.updated_at),
            }
            for binding in scope_bindings
        ],
        "retention_policies": [
            {
                "id": policy.id,
                "scope_key": policy.scope_key,
                "policy_kind": policy.policy_kind,
                "pattern": policy.pattern,
                "retention_days": policy.retention_days,
                "state": policy.lifecycle_state,
                "reason": policy.reason,
                "created_at": to_rfc3339(policy.created_at),
                "updated_at": to_rfc3339(policy.updated_at),
            }
            for policy in retention_policies
        ],
        "sensitivity_labels": [
            {
                "id": label.id,
                "canonical_table": label.canonical_table,
                "canonical_id": label.canonical_id,
                "label": label.label,
                "actor_id": label.actor_id,
                "state": label.lifecycle_state,
                "reason": label.reason,
                "created_at": to_rfc3339(label.created_at),
                "updated_at": to_rfc3339(label.updated_at),
            }
            for label in sensitivity_labels
        ],
        "export_artifacts": [
            {
                "id": artifact.id,
                "scope_key": artifact.scope_key,
                "export_format": artifact.export_format,
                "status": artifact.status,
                "projection_version": artifact.projection_version,
                "redaction_posture": artifact.redaction_posture,
                "source_counts": artifact.source_counts,
                "source_memory_versions": artifact.source_memory_versions,
                "source_projection_versions": artifact.source_projection_versions,
                "created_at": to_rfc3339(artifact.created_at),
                "updated_at": to_rfc3339(artifact.updated_at),
            }
            for artifact in export_artifacts
        ],
        "eval_runs": [
            {
                "id": run.id,
                "eval_name": run.eval_name,
                "status": run.status,
                "metrics": run.metrics,
                "created_at": to_rfc3339(run.created_at),
                "updated_at": to_rfc3339(run.updated_at),
            }
            for run in eval_runs
        ],
        "projection_health": {
            "projection_version": MEMORY_PROJECTION_VERSION,
            "pending_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state == "pending")
            )
            or 0,
            "failed_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state.in_(("failed", "dead_letter")))
            )
            or 0,
            "running_jobs": db.scalar(
                select(func.count())
                .select_from(MemoryProjectionJobRecord)
                .where(MemoryProjectionJobRecord.lifecycle_state == "running")
            )
            or 0,
        },
    }


def list_memory_events(
    db: Session,
    *,
    scope_key: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the unified memory event log, newest first, optionally filtered by
    scope, event type, and a right-open ``[since, until)`` time range."""
    query = select(MemoryEventRecord)
    if scope_key is not None:
        query = query.where(MemoryEventRecord.scope_key == scope_key)
    if event_type is not None:
        query = query.where(MemoryEventRecord.event_type == event_type)
    if since is not None:
        query = query.where(MemoryEventRecord.created_at >= since)
    if until is not None:
        query = query.where(MemoryEventRecord.created_at < until)
    events = db.scalars(
        query.order_by(MemoryEventRecord.created_at.desc(), MemoryEventRecord.id.desc()).limit(
            limit
        )
    ).all()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "scope_key": event.scope_key,
            "actor_id": event.actor_id,
            "entry_path": event.entry_path,
            "subject_refs": event.subject_refs,
            "payload": event.payload,
            "source_turn_id": event.source_turn_id,
            "created_at": to_rfc3339(event.created_at),
        }
        for event in events
    ]


def _validated_memory_curation(
    raw_payload: Any,
    *,
    candidate_ids: set[str],
    candidate_kinds: dict[str, str],
    max_selected: int,
    model: str,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="memory curation model returned a non-object JSON value",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
        )

    selected_raw = raw_payload.get("selected_memories")
    omitted_raw = raw_payload.get("omitted_memories")
    rationale = raw_payload.get("rationale")
    uncertainty = raw_payload.get("uncertainty")
    confidence = raw_payload.get("confidence")
    if not (
        isinstance(selected_raw, list)
        and isinstance(omitted_raw, list)
        and isinstance(rationale, str)
        and isinstance(uncertainty, str)
        and isinstance(confidence, int | float)
    ):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_SCHEMA",
            safe_reason="memory curation JSON missing required fields",
            retryable=False,
            parse_status="schema_invalid",
            validation_status="invalid",
        )

    selected: list[dict[str, str]] = []
    selected_ids: list[str] = []
    for item in selected_raw:
        if not isinstance(item, dict):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation selected_memories entries must be objects",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        memory_id = item.get("id")
        item_rationale = item.get("rationale")
        if not isinstance(memory_id, str) or not isinstance(item_rationale, str):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation selected memory missing id or rationale",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        if memory_id not in candidate_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation selected an unknown memory id",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        if memory_id in selected_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation selected duplicate memory ids",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        selected_ids.append(memory_id)
        selected.append(
            {
                "id": memory_id,
                "kind": candidate_kinds[memory_id],
                "rationale": _clean_text(item_rationale, max_chars=300),
            }
        )

    if len(selected) > max_selected:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation selected too many memories",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )

    omitted: list[dict[str, str]] = []
    omitted_ids: list[str] = []
    for item in omitted_raw:
        if not isinstance(item, dict):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation omitted_memories entries must be objects",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        memory_id = item.get("id")
        reason = item.get("reason")
        if not isinstance(memory_id, str) or not isinstance(reason, str):
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_SCHEMA",
                safe_reason="memory curation omitted memory missing id or reason",
                retryable=False,
                parse_status="schema_invalid",
                validation_status="invalid",
            )
        if memory_id not in candidate_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation omitted an unknown memory id",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        if memory_id in omitted_ids:
            raise AIJudgmentFailure(
                code="E_AI_JUDGMENT_VALIDATION",
                safe_reason="memory curation omitted duplicate memory ids",
                retryable=False,
                parse_status="parsed",
                validation_status="invalid",
            )
        omitted_ids.append(memory_id)
        omitted.append(
            {"id": memory_id, "kind": candidate_kinds[memory_id], "reason": _clean_text(reason)}
        )

    if set(selected_ids) | set(omitted_ids) != candidate_ids:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation must account for every memory candidate",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )
    if set(selected_ids).intersection(omitted_ids):
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_VALIDATION",
            safe_reason="memory curation cannot select and omit the same memory",
            retryable=False,
            parse_status="parsed",
            validation_status="invalid",
        )

    return {
        "selected_memories": selected,
        "omitted_memories": omitted,
        "rationale": _clean_text(rationale, max_chars=700),
        "uncertainty": _clean_text(uncertainty, max_chars=500),
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "model": model,
        "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
        "parse_status": "parsed",
    }


def _curate_memory_context_with_model(
    *,
    user_message: str,
    history: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    max_selected: int,
    settings: AppSettings,
) -> dict[str, Any]:
    if settings.openai_api_key is None:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_CREDENTIALS",
            safe_reason="AI memory curation requires ARIEL_OPENAI_API_KEY",
            retryable=False,
            parse_status="missing_output",
            validation_status="not_validated",
        )

    prompt = (
        "You are Ariel's memory curator. Select only memories that matter for the "
        "current turn. The candidates are a fused multi-signal pool; each carries a "
        "kind (semantic_assertion, episode, reasoning_trace, action_trace, procedure, "
        "project_state, hot_index, topic, negative_memory, conflict) and a "
        "retrieval_features vector (rrf_score, signal_ranks, vector_distance, "
        "lexical_rank, salience_score, user_priority, source_trust, "
        "effective_confidence, verification_age_days, conflict_status, validity, "
        "topic_membership). Reciprocal Rank Fusion produced the pool order; it is "
        "transport order, not relevance — use the user request, recent history, "
        "memory values, evidence, validity, conflicts, decay, and provenance to judge "
        "relevance. A candidate whose conflict_status is open is unresolved: never "
        "treat it as settled. Return JSON only with selected_memories, "
        "omitted_memories, rationale, uncertainty, and confidence. selected_memories "
        "must contain objects with id and rationale. omitted_memories must contain "
        "every unselected candidate with id and reason — selected and omitted together "
        "must account for every candidate. Select at most the provided max_selected."
    )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.model_name,
                "input": [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
                                "max_selected": max_selected,
                                "user_request": user_message,
                                "recent_history": list(history),
                                "memory_candidates": list(candidates),
                            }
                        ),
                    },
                ],
                "store": False,
                "text": {"verbosity": "low"},
            },
            timeout=settings.model_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_TIMEOUT",
            safe_reason="memory curation model timed out",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    except httpx.HTTPError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory curation model network request failed",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    if response.status_code >= 400:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason=f"memory curation model returned HTTP {response.status_code}",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory curation provider returned invalid JSON",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    provider_response_id = response_payload.get("id")
    provider_response_id = provider_response_id if isinstance(provider_response_id, str) else None
    text = _extract_output_text(response_payload.get("output"))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_INVALID_JSON",
            safe_reason="memory curation model returned malformed JSON",
            retryable=False,
            parse_status="invalid_json",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        ) from exc
    try:
        curation = _validated_memory_curation(
            payload,
            candidate_ids={str(candidate["id"]) for candidate in candidates},
            candidate_kinds={
                str(candidate["id"]): str(candidate.get("kind") or "memory")
                for candidate in candidates
            },
            max_selected=max_selected,
            model=settings.model_name,
        )
    except AIJudgmentFailure as exc:
        exc.provider_response_id = provider_response_id
        raise
    curation["provider_response_id"] = provider_response_id
    return curation


def search_memory(
    db: Session,
    *,
    query: str,
    limit: int,
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
    scope_key: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    actor_id: str | None = None,
) -> list[dict[str, Any]]:
    memory_context, _ = build_memory_context(
        db,
        user_message=query,
        max_recalled_assertions=limit,
        settings=settings,
        current_session_id=current_session_id,
        scope_key=scope_key,
        thread_id=thread_id,
        proactive_case_id=proactive_case_id,
        actor_id=actor_id,
    )
    recall_window = memory_context.get("recall_window")
    if not isinstance(recall_window, dict):
        return []
    selected_rationales = {
        item["id"]: item.get("rationale")
        for item in recall_window.get("selected_memories", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    results = []
    for item in recall_window.get("candidate_memories", []):
        if isinstance(item, dict) and item.get("id") in selected_rationales:
            memory_id = item.get("id")
            kind = item.get("kind")
            if isinstance(memory_id, str) and isinstance(kind, str):
                results.append(
                    {
                        "id": memory_id,
                        "kind": kind,
                        "rationale": selected_rationales[memory_id],
                        "value": item.get("value") or item.get("summary") or item.get("content"),
                        "evidence_refs": item.get("evidence_refs", []),
                        "retrieval_features": item.get("retrieval_features", {}),
                        "conflict_status": item.get("conflict_status"),
                        "projection_version": item.get(
                            "projection_version", MEMORY_PROJECTION_VERSION
                        ),
                    }
                )
    return results[:limit]


def _projection_health(db: Session) -> dict[str, Any]:
    """Honest projection-health counters for recall diagnostics: failed and
    dead-lettered projection jobs, and the number of active assertions whose
    keyword projection lags the assertion's canonical memory version."""
    failed = (
        db.scalar(
            select(func.count())
            .select_from(MemoryProjectionJobRecord)
            .where(MemoryProjectionJobRecord.lifecycle_state == "failed")
        )
        or 0
    )
    dead_letter = (
        db.scalar(
            select(func.count())
            .select_from(MemoryProjectionJobRecord)
            .where(MemoryProjectionJobRecord.lifecycle_state == "dead_letter")
        )
        or 0
    )
    canonical_version = (
        select(
            MemoryVersionRecord.canonical_id.label("assertion_id"),
            func.max(MemoryVersionRecord.version).label("version"),
        )
        .where(MemoryVersionRecord.canonical_table == "memory_assertions")
        .group_by(MemoryVersionRecord.canonical_id)
        .subquery()
    )
    stale = (
        db.scalar(
            select(func.count())
            .select_from(MemoryAssertionRecord)
            .join(
                MemoryKeywordProjectionRecord,
                (MemoryKeywordProjectionRecord.canonical_table == "memory_assertions")
                & (MemoryKeywordProjectionRecord.canonical_id == MemoryAssertionRecord.id),
            )
            .join(
                canonical_version,
                canonical_version.c.assertion_id == MemoryAssertionRecord.id,
            )
            .where(
                MemoryAssertionRecord.lifecycle_state == "active",
                MemoryKeywordProjectionRecord.source_memory_version < canonical_version.c.version,
            )
        )
        or 0
    )
    return {
        "failed_projection_jobs": failed,
        "dead_letter_projection_jobs": dead_letter,
        "stale_projection_count": stale,
    }


# Each retrieval signal and the fused pool address candidates by a canonical
# (table, id) ref. These constants name the canonical tables every signal and the
# kind map below agree on.
_TABLE_ASSERTIONS = "memory_assertions"
_TABLE_EPISODES = "memory_episodes"
_TABLE_REASONING_TRACES = "memory_reasoning_traces"
_TABLE_ACTION_TRACES = "memory_action_traces"
_TABLE_PROCEDURES = "memory_procedures"
_TABLE_SNAPSHOTS = "project_state_snapshots"
_TABLE_CONTEXT_BLOCKS = "memory_context_blocks"
_TABLE_CONFLICT_SETS = "memory_conflict_sets"


def _fuse_candidates(
    signal_rankings: Mapping[str, Sequence[tuple[str, str]]],
    *,
    k: int,
) -> list[tuple[tuple[str, str], float, dict[str, int]]]:
    """Reciprocal Rank Fusion. Returns (canonical_ref, fused_score, per-signal
    rank) sorted by score desc then ref asc. Deterministic for fixed input: the
    signals are visited in sorted name order and ties break on the ref."""
    scores: dict[tuple[str, str], float] = {}
    ranks: dict[tuple[str, str], dict[str, int]] = {}
    for signal, ranking in sorted(signal_rankings.items()):
        for rank, ref in enumerate(ranking):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (k + rank + 1)
            ranks.setdefault(ref, {})[signal] = rank + 1
    return sorted(
        ((ref, scores[ref], ranks[ref]) for ref in scores),
        key=lambda item: (-item[1], item[0]),
    )


def _vector_signal(
    db: Session,
    *,
    user_message: str,
    settings: AppSettings,
    limit: int,
) -> tuple[list[tuple[str, str]], dict[str, float]]:
    """pgvector cosine ranking over memory_embedding_projections, bounded by the
    configured distance ceiling. Returns the ranked assertion refs and the
    distance feature per assertion id."""
    embedding_count = (
        db.scalar(
            select(func.count())
            .select_from(MemoryEmbeddingProjectionRecord)
            .where(
                MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
                MemoryEmbeddingProjectionRecord.embedding_provider
                == settings.memory_embedding_provider,
                MemoryEmbeddingProjectionRecord.embedding_model == settings.memory_embedding_model,
                MemoryEmbeddingProjectionRecord.embedding_dimensions
                == settings.memory_embedding_dimensions,
            )
        )
        or 0
    )
    if not embedding_count:
        return [], {}
    query_vector = embed_memory_text(user_message, settings=settings)
    distance = MemoryEmbeddingProjectionRecord.embedding.cosine_distance(query_vector)
    rows = db.execute(
        select(MemoryEmbeddingProjectionRecord.assertion_id, distance.label("distance"))
        .where(
            MemoryEmbeddingProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            MemoryEmbeddingProjectionRecord.embedding_provider
            == settings.memory_embedding_provider,
            MemoryEmbeddingProjectionRecord.embedding_model == settings.memory_embedding_model,
            MemoryEmbeddingProjectionRecord.embedding_dimensions
            == settings.memory_embedding_dimensions,
            distance <= settings.memory_vector_distance_ceiling,
        )
        .order_by(distance.asc(), MemoryEmbeddingProjectionRecord.assertion_id.asc())
        .limit(limit)
    ).all()
    ranking: list[tuple[str, str]] = []
    distances: dict[str, float] = {}
    for assertion_id, value in rows:
        if value is None:
            continue
        ranking.append((_TABLE_ASSERTIONS, assertion_id))
        distances[assertion_id] = float(value)
    return ranking, distances


def _lexical_signal(
    db: Session,
    *,
    user_message: str,
    limit: int,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Postgres full-text ranking. Assertions rank by ts_rank_cd over the
    persisted search_vector; every other kind ranks by ts_rank_cd over an inline
    to_tsvector of its own text columns. Returns the merged ranked refs and the
    per-id lexical rank feature."""
    tsquery = func.plainto_tsquery("english", user_message)
    scored: list[tuple[tuple[str, str], float]] = []

    # Assertions use the persisted search_vector tsvector column.
    keyword_rank = func.ts_rank_cd(MemoryKeywordProjectionRecord.search_vector, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryKeywordProjectionRecord.canonical_id, keyword_rank.label("rank"))
        .where(
            MemoryKeywordProjectionRecord.canonical_table == _TABLE_ASSERTIONS,
            MemoryKeywordProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            MemoryKeywordProjectionRecord.search_vector.op("@@")(tsquery),
        )
        .order_by(keyword_rank.desc(), MemoryKeywordProjectionRecord.canonical_id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_ASSERTIONS, str(canonical_id)), float(rank)))

    # Every other kind is ranked by an inline to_tsvector of its own text columns.
    episode_doc = func.to_tsvector(
        "english",
        func.concat_ws(
            " ",
            MemoryEpisodeRecord.title,
            MemoryEpisodeRecord.summary,
            MemoryEpisodeRecord.outcome,
        ),
    )
    episode_rank = func.ts_rank_cd(episode_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryEpisodeRecord.id, episode_rank.label("rank"))
        .where(
            MemoryEpisodeRecord.lifecycle_state == "active",
            episode_doc.op("@@")(tsquery),
        )
        .order_by(episode_rank.desc(), MemoryEpisodeRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_EPISODES, str(canonical_id)), float(rank)))

    trace_doc = func.to_tsvector(
        "english",
        func.concat_ws(
            " ",
            MemoryReasoningTraceRecord.task_summary,
            MemoryReasoningTraceRecord.trace_summary,
        ),
    )
    trace_rank = func.ts_rank_cd(trace_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryReasoningTraceRecord.id, trace_rank.label("rank"))
        .where(
            MemoryReasoningTraceRecord.lifecycle_state == "active",
            trace_doc.op("@@")(tsquery),
        )
        .order_by(trace_rank.desc(), MemoryReasoningTraceRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_REASONING_TRACES, str(canonical_id)), float(rank)))

    action_doc = func.to_tsvector("english", MemoryActionTraceRecord.summary)
    action_rank = func.ts_rank_cd(action_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryActionTraceRecord.id, action_rank.label("rank"))
        .where(
            MemoryActionTraceRecord.lifecycle_state == "active",
            action_doc.op("@@")(tsquery),
        )
        .order_by(action_rank.desc(), MemoryActionTraceRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_ACTION_TRACES, str(canonical_id)), float(rank)))

    procedure_doc = func.to_tsvector(
        "english",
        func.concat_ws(" ", MemoryProcedureRecord.title, MemoryProcedureRecord.instruction),
    )
    procedure_rank = func.ts_rank_cd(procedure_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryProcedureRecord.id, procedure_rank.label("rank"))
        .where(
            MemoryProcedureRecord.lifecycle_state == "active",
            procedure_doc.op("@@")(tsquery),
        )
        .order_by(procedure_rank.desc(), MemoryProcedureRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_PROCEDURES, str(canonical_id)), float(rank)))

    snapshot_doc = func.to_tsvector("english", ProjectStateSnapshotRecord.summary)
    snapshot_rank = func.ts_rank_cd(snapshot_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(ProjectStateSnapshotRecord.id, snapshot_rank.label("rank"))
        .where(
            ProjectStateSnapshotRecord.lifecycle_state == "active",
            snapshot_doc.op("@@")(tsquery),
        )
        .order_by(snapshot_rank.desc(), ProjectStateSnapshotRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_SNAPSHOTS, str(canonical_id)), float(rank)))

    block_doc = func.to_tsvector("english", MemoryContextBlockRecord.content)
    block_rank = func.ts_rank_cd(block_doc, tsquery)
    for canonical_id, rank in db.execute(
        select(MemoryContextBlockRecord.id, block_rank.label("rank"))
        .where(
            MemoryContextBlockRecord.lifecycle_state == "active",
            block_doc.op("@@")(tsquery),
        )
        .order_by(block_rank.desc(), MemoryContextBlockRecord.id.asc())
        .limit(limit)
    ).all():
        scored.append(((_TABLE_CONTEXT_BLOCKS, str(canonical_id)), float(rank)))

    scored.sort(key=lambda item: (-item[1], item[0]))
    ranking = [ref for ref, _ in scored]
    lexical_rank = {ref[1]: index + 1 for index, (ref, _) in enumerate(scored)}
    return ranking, lexical_rank


def _entity_signal(
    db: Session,
    *,
    query_terms: set[str],
    limit: int,
) -> tuple[list[tuple[str, str]], dict[str, list[str]], set[str]]:
    """Match query terms to memory_entities, then resolve assertion refs through
    memory_entity_projections. Returns the ranked assertion refs, the matched
    entity ids per assertion id, and the set of matched entity ids (for the graph
    signal to expand)."""
    if not query_terms:
        return [], {}, set()
    matched_entity_ids = sorted(
        entity.id
        for entity in db.scalars(
            select(MemoryEntityRecord).order_by(MemoryEntityRecord.id.asc())
        ).all()
        if query_terms.intersection(set(_terms(f"{entity.entity_key} {entity.display_name}")))
    )
    if not matched_entity_ids:
        return [], {}, set()
    entity_ids_by_assertion: dict[str, list[str]] = {}
    ranking: list[tuple[str, str]] = []
    for row in db.scalars(
        select(MemoryEntityProjectionRecord)
        .where(
            MemoryEntityProjectionRecord.canonical_table == _TABLE_ASSERTIONS,
            MemoryEntityProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            MemoryEntityProjectionRecord.entity_id.in_(matched_entity_ids),
        )
        .order_by(
            MemoryEntityProjectionRecord.canonical_id.asc(),
            MemoryEntityProjectionRecord.entity_id.asc(),
        )
        .limit(limit)
    ).all():
        ref = (_TABLE_ASSERTIONS, row.canonical_id)
        if ref not in ranking:
            ranking.append(ref)
        entity_ids_by_assertion.setdefault(row.canonical_id, []).append(row.entity_id)
    return ranking, entity_ids_by_assertion, set(matched_entity_ids)


def _graph_signal(
    db: Session,
    *,
    seed_entity_ids: set[str],
    limit: int,
) -> list[tuple[str, str]]:
    """Entities reachable from a query-matched entity within the depth-3 graph
    projection, then the assertion refs whose subject is a reachable entity.
    Ranked by hop distance (nearer first)."""
    if not seed_entity_ids:
        return []
    reachable: list[tuple[int, str]] = []
    seen: set[str] = set()
    for row in db.scalars(
        select(MemoryGraphProjectionRecord)
        .where(
            MemoryGraphProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            or_(
                MemoryGraphProjectionRecord.source_entity_id.in_(sorted(seed_entity_ids)),
                MemoryGraphProjectionRecord.target_entity_id.in_(sorted(seed_entity_ids)),
            ),
        )
        .order_by(
            MemoryGraphProjectionRecord.distance.asc(),
            MemoryGraphProjectionRecord.id.asc(),
        )
    ).all():
        for entity_id in (row.source_entity_id, row.target_entity_id):
            if entity_id not in seed_entity_ids and entity_id not in seen:
                seen.add(entity_id)
                reachable.append((row.distance, entity_id))
    if not reachable:
        return []
    ordered_entity_ids = [entity_id for _, entity_id in reachable]
    assertions = db.scalars(
        select(MemoryAssertionRecord)
        .where(MemoryAssertionRecord.subject_entity_id.in_(ordered_entity_ids))
        .order_by(MemoryAssertionRecord.id.asc())
    ).all()
    by_entity: dict[str, list[str]] = {}
    for assertion in assertions:
        by_entity.setdefault(assertion.subject_entity_id, []).append(assertion.id)
    ranking: list[tuple[str, str]] = []
    for entity_id in ordered_entity_ids:
        for assertion_id in by_entity.get(entity_id, []):
            ranking.append((_TABLE_ASSERTIONS, assertion_id))
    return ranking[:limit]


def _symbol_signal(
    db: Session,
    *,
    query_terms: set[str],
    limit: int,
) -> tuple[list[tuple[str, str]], dict[str, list[dict[str, str | None]]]]:
    """Repo-scoped identifier/path tokens via memory_symbol_projections. Returns
    the ranked assertion refs and the matched symbol features per assertion id."""
    if not query_terms:
        return [], {}
    ranking: list[tuple[str, str]] = []
    symbol_features: dict[str, list[dict[str, str | None]]] = {}
    for row in db.scalars(
        select(MemorySymbolProjectionRecord)
        .where(
            MemorySymbolProjectionRecord.canonical_table == _TABLE_ASSERTIONS,
            MemorySymbolProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .order_by(
            MemorySymbolProjectionRecord.canonical_id.asc(),
            MemorySymbolProjectionRecord.id.asc(),
        )
    ).all():
        if not query_terms.intersection(set(_terms(f"{row.repo_key} {row.symbol} {row.path}"))):
            continue
        ref = (_TABLE_ASSERTIONS, row.canonical_id)
        if ref not in ranking:
            ranking.append(ref)
        symbol_features.setdefault(row.canonical_id, []).append(
            {
                "repo_key": row.repo_key,
                "symbol": row.symbol,
                "path": row.path,
                "language": row.language,
            }
        )
    return ranking[:limit], symbol_features


def _temporal_signal(db: Session, *, now: datetime, limit: int) -> list[tuple[str, str]]:
    """Assertions and episodes whose bounded validity interval contains now, via
    memory_temporal_projections. A row is a temporal match only when it has an
    explicit valid_to upper bound — a perpetually-valid memory (valid_to is null)
    carries no temporal signal and is left to the relevance signals. Activation
    stamps valid_from on every assertion, so valid_from alone is not a signal."""
    ranking: list[tuple[str, str]] = []
    for row in db.scalars(
        select(MemoryTemporalProjectionRecord)
        .where(
            MemoryTemporalProjectionRecord.projection_version == MEMORY_PROJECTION_VERSION,
            MemoryTemporalProjectionRecord.temporal_kind == "validity",
            # Only genuinely time-scoped memory: an explicit upper bound.
            MemoryTemporalProjectionRecord.valid_to.is_not(None),
            MemoryTemporalProjectionRecord.valid_to > now,
            or_(
                MemoryTemporalProjectionRecord.valid_from.is_(None),
                MemoryTemporalProjectionRecord.valid_from <= now,
            ),
        )
        .order_by(
            MemoryTemporalProjectionRecord.valid_from.desc().nulls_last(),
            MemoryTemporalProjectionRecord.canonical_id.asc(),
        )
        .limit(limit)
    ).all():
        ranking.append((row.canonical_table, row.canonical_id))
    return ranking


def _recency_signal(db: Session, *, per_kind_limit: int) -> list[tuple[str, str]]:
    """Most-recently-updated active rows of every non-assertion kind, as a
    baseline signal so episodes, traces, procedures, project state, context
    blocks, and open conflicts are all represented in the pool. Assertions
    (including negative memory) are deliberately excluded: a semantic fact enters
    the pool only through a relevance signal, never through pure recency."""
    ranking: list[tuple[str, str]] = []
    for episode in db.scalars(
        select(MemoryEpisodeRecord)
        .where(MemoryEpisodeRecord.lifecycle_state == "active")
        .order_by(MemoryEpisodeRecord.occurred_at.desc(), MemoryEpisodeRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_EPISODES, episode.id))
    for reasoning_trace in db.scalars(
        select(MemoryReasoningTraceRecord)
        .where(MemoryReasoningTraceRecord.lifecycle_state == "active")
        .order_by(MemoryReasoningTraceRecord.updated_at.desc(), MemoryReasoningTraceRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_REASONING_TRACES, reasoning_trace.id))
    for action_trace in db.scalars(
        select(MemoryActionTraceRecord)
        .where(MemoryActionTraceRecord.lifecycle_state == "active")
        .order_by(MemoryActionTraceRecord.updated_at.desc(), MemoryActionTraceRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_ACTION_TRACES, action_trace.id))
    for procedure in db.scalars(
        select(MemoryProcedureRecord)
        .where(
            MemoryProcedureRecord.lifecycle_state == "active",
            MemoryProcedureRecord.review_state.in_(("approved", "auto_approved")),
        )
        .order_by(MemoryProcedureRecord.updated_at.desc(), MemoryProcedureRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_PROCEDURES, procedure.id))
    for snapshot in db.scalars(
        select(ProjectStateSnapshotRecord)
        .where(ProjectStateSnapshotRecord.lifecycle_state == "active")
        .order_by(ProjectStateSnapshotRecord.updated_at.desc(), ProjectStateSnapshotRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_SNAPSHOTS, snapshot.id))
    for block in db.scalars(
        select(MemoryContextBlockRecord)
        .where(
            MemoryContextBlockRecord.block_type.in_(("hot_index", "topic")),
            MemoryContextBlockRecord.lifecycle_state == "active",
            MemoryContextBlockRecord.projection_version == MEMORY_PROJECTION_VERSION,
        )
        .order_by(MemoryContextBlockRecord.updated_at.desc(), MemoryContextBlockRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_CONTEXT_BLOCKS, block.id))
    for conflict in db.scalars(
        select(MemoryConflictSetRecord)
        .where(MemoryConflictSetRecord.lifecycle_state == "open")
        .order_by(MemoryConflictSetRecord.updated_at.desc(), MemoryConflictSetRecord.id.asc())
        .limit(per_kind_limit)
    ).all():
        ranking.append((_TABLE_CONFLICT_SETS, conflict.id))
    return ranking


def _effective_confidence(assertion: MemoryAssertionRecord, *, now: datetime) -> float:
    """Confidence decayed by the predicate's declared half-life. A recall feature,
    never a filter: confidence * 0.5 ** (age_days / half_life)."""
    spec = resolve_predicate_spec(assertion.predicate)
    if spec.decay_half_life_days is None:
        return assertion.confidence
    age_days = max(0.0, (now - assertion.last_verified_at).total_seconds() / 86_400.0)
    return assertion.confidence * 0.5 ** (age_days / spec.decay_half_life_days)


def _topic_membership_for_ids(db: Session, canonical_ids: Sequence[str]) -> dict[str, list[str]]:
    """The active topic ids each canonical row belongs to, for the recall feature
    vector's topic_membership field."""
    if not canonical_ids:
        return {}
    membership: dict[str, list[str]] = {}
    for row in db.scalars(
        select(MemoryTopicMemberRecord)
        .join(MemoryTopicRecord, MemoryTopicRecord.id == MemoryTopicMemberRecord.topic_id)
        .where(
            MemoryTopicRecord.lifecycle_state == "active",
            MemoryTopicMemberRecord.canonical_id.in_(list(canonical_ids)),
        )
        .order_by(MemoryTopicMemberRecord.topic_id.asc())
    ).all():
        membership.setdefault(row.canonical_id, []).append(row.topic_id)
    return membership


def _non_assertion_features(
    rrf_score: float,
    signal_ranks: dict[str, int],
    lexical_rank: int | None,
    topic_membership: list[str],
) -> dict[str, Any]:
    """The recall feature vector for a non-assertion candidate. The assertion-only
    fields (vector distance, decay, validity) are null; the fused-pool fields are
    always present so curation sees a uniform feature shape."""
    return {
        "rrf_score": rrf_score,
        "signal_ranks": dict(sorted(signal_ranks.items())),
        "vector_distance": None,
        "lexical_rank": lexical_rank,
        "salience_score": None,
        "user_priority": None,
        "source_trust": "reviewed_memory",
        "effective_confidence": None,
        "verification_age_days": None,
        "conflict_status": {"state": "none", "conflict_ids": []},
        "validity": {"valid_from": None, "valid_to": None},
        "topic_membership": topic_membership,
    }


def build_memory_context(
    db: Session,
    *,
    user_message: str,
    max_recalled_assertions: int,
    settings: AppSettings | None = None,
    current_session_id: str | None = None,
    scope_key: str | None = None,
    thread_id: str | None = None,
    proactive_case_id: str | None = None,
    actor_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assemble the recall context via a deterministic Reciprocal Rank Fusion
    pipeline: resolve policy, run each retrieval signal, fuse the rankings into
    one bounded candidate pool, apply the deterministic rails, attach a feature
    vector to every survivor, then hand the pool to AI curation."""
    resolved_settings = settings or AppSettings()
    now = datetime.now(UTC)
    context: dict[str, Any]
    event_payload: dict[str, Any]
    query_terms = set(_terms(user_message))
    effective_scope_key = scope_key
    if effective_scope_key is None and query_terms:
        effective_scope_key = _matching_scope_key_for_text(db, text=user_message)
    scope_aliases: set[str] = set()
    if effective_scope_key is not None and effective_scope_key != "global":
        scope_aliases.add(effective_scope_key)
        if effective_scope_key.startswith("project:"):
            scope_aliases.add(effective_scope_key.removeprefix("project:"))
        elif effective_scope_key.startswith("repo:"):
            scope_aliases.add(effective_scope_key.removeprefix("repo:"))
        else:
            scope_aliases.add(f"project:{effective_scope_key}")
            scope_aliases.add(f"repo:{effective_scope_key}")

    # (a) Resolve policy. Recall always resolves policy: with no session it
    # resolves the project/repo/user chain; recall is never unrestricted. A
    # disallowed policy fails closed to a typed empty context.
    project_key, repo_key = scope_keys_for_policy(effective_scope_key)
    policy = resolve_memory_policy(
        db,
        operation="recall",
        now=now,
        session_id=current_session_id,
        thread_id=thread_id,
        proactive_case_id=proactive_case_id,
        project_key=project_key,
        repo_key=repo_key,
        actor_id=actor_id,
    )
    memory_policy = policy.as_dict()
    if not policy.allowed:
        context = {
            "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
            "projection_version": MEMORY_PROJECTION_VERSION,
            "hot_index": [],
            "topic_index": [],
            "pinned_core": [],
            "project_state": [],
            "commitments_and_decisions": [],
            "semantic_assertions": [],
            "episodic_evidence": [],
            "procedural_memory": [],
            "action_traces": [],
            "reasoning_traces": [],
            "negative_memory": [],
            "conflicts": [],
            "recall_window": {
                "max_selected_memories": max_recalled_assertions,
                "selected_memory_count": 0,
                "memory_candidate_count": 0,
                "omitted_memory_count": 0,
                "rails_excluded_count": 0,
                "selected_memory_ids": [],
                "selected_memories": [],
                "omitted_memories": [],
                "candidate_memory_ids": [],
                "candidate_memories": [],
                "rails_excluded": [],
                "curation_rationale": f"Memory recall skipped: {policy.reason}.",
                "curation_uncertainty": "",
                "curation_confidence": 1.0,
                "curation_model": None,
                "curation_prompt_version": MEMORY_CURATION_PROMPT_VERSION,
                "curation_parse_status": "not_required_no_candidates",
                "curation_provider_response_id": None,
            },
            "memory_policy": memory_policy,
            "projection_health": {
                "projection_version": MEMORY_PROJECTION_VERSION,
                "selected_assertion_count": 0,
                "selected_memory_count": 0,
                **_projection_health(db),
            },
        }
        event_payload = {
            "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
            "projection_version": MEMORY_PROJECTION_VERSION,
            **context["recall_window"],
            "conflict_ids": [],
            "memory_policy": memory_policy,
        }
        return context, event_payload

    candidate_limit = max(50, max_recalled_assertions * 8)

    # (b) Run each retrieval signal. Every signal returns a best-first ranked list
    # of (canonical_table, canonical_id) refs.
    vector_ranking, vector_distance_by_id = _vector_signal(
        db, user_message=user_message, settings=resolved_settings, limit=candidate_limit
    )
    lexical_ranking, lexical_rank_by_id = _lexical_signal(
        db, user_message=user_message, limit=candidate_limit
    )
    entity_ranking, entity_ids_by_id, matched_entity_ids = _entity_signal(
        db, query_terms=query_terms, limit=candidate_limit
    )
    graph_ranking = _graph_signal(db, seed_entity_ids=matched_entity_ids, limit=candidate_limit)
    symbol_ranking, symbol_features_by_id = _symbol_signal(
        db, query_terms=query_terms, limit=candidate_limit
    )
    temporal_ranking = _temporal_signal(db, now=now, limit=candidate_limit)
    recency_ranking = _recency_signal(db, per_kind_limit=max(8, max_recalled_assertions))
    signal_rankings: dict[str, list[tuple[str, str]]] = {
        "vector": vector_ranking,
        "lexical": lexical_ranking,
        "entity": entity_ranking,
        "graph": graph_ranking,
        "symbol": symbol_ranking,
        "temporal": temporal_ranking,
        "recency": recency_ranking,
    }

    # (c) Fuse the rankings into one deterministic pool: RRF with a fixed k, ties
    # broken by ref.
    fused = _fuse_candidates(signal_rankings, k=resolved_settings.memory_rrf_k)

    # Load every canonical row a fused ref points at, keyed by ref.
    fused_refs = [ref for ref, _, _ in fused]
    ids_by_table: dict[str, list[str]] = {}
    for table, canonical_id in fused_refs:
        ids_by_table.setdefault(table, []).append(canonical_id)
    assertions_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryAssertionRecord).where(
                MemoryAssertionRecord.id.in_(ids_by_table.get(_TABLE_ASSERTIONS, []) or [""])
            )
        ).all()
    }
    episodes_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryEpisodeRecord).where(
                MemoryEpisodeRecord.id.in_(ids_by_table.get(_TABLE_EPISODES, []) or [""])
            )
        ).all()
    }
    reasoning_traces_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryReasoningTraceRecord).where(
                MemoryReasoningTraceRecord.id.in_(
                    ids_by_table.get(_TABLE_REASONING_TRACES, []) or [""]
                )
            )
        ).all()
    }
    action_traces_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryActionTraceRecord).where(
                MemoryActionTraceRecord.id.in_(ids_by_table.get(_TABLE_ACTION_TRACES, []) or [""])
            )
        ).all()
    }
    procedures_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryProcedureRecord).where(
                MemoryProcedureRecord.id.in_(ids_by_table.get(_TABLE_PROCEDURES, []) or [""])
            )
        ).all()
    }
    snapshots_by_id = {
        row.id: row
        for row in db.scalars(
            select(ProjectStateSnapshotRecord).where(
                ProjectStateSnapshotRecord.id.in_(ids_by_table.get(_TABLE_SNAPSHOTS, []) or [""])
            )
        ).all()
    }
    context_blocks_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryContextBlockRecord).where(
                MemoryContextBlockRecord.id.in_(ids_by_table.get(_TABLE_CONTEXT_BLOCKS, []) or [""])
            )
        ).all()
    }
    conflict_sets_by_id = {
        row.id: row
        for row in db.scalars(
            select(MemoryConflictSetRecord).where(
                MemoryConflictSetRecord.id.in_(ids_by_table.get(_TABLE_CONFLICT_SETS, []) or [""])
            )
        ).all()
    }

    # Sensitivity labels and the open-conflict membership are rail and feature
    # inputs; gather them once for every fused ref.
    fused_ids = [canonical_id for _, canonical_id in fused_refs]
    secret_labelled_ids = {
        row.canonical_id
        for row in db.scalars(
            select(MemorySensitivityLabelRecord).where(
                MemorySensitivityLabelRecord.lifecycle_state == "active",
                MemorySensitivityLabelRecord.label.in_(
                    ("secret", "regulated", "source_confidential")
                ),
                MemorySensitivityLabelRecord.canonical_id.in_(fused_ids or [""]),
            )
        ).all()
    }
    assertion_ids = list(assertions_by_id)
    open_conflict_ids_by_assertion_id: dict[str, list[str]] = {}
    for conflict_id, member_assertion_id in db.execute(
        select(MemoryConflictMemberRecord.conflict_set_id, MemoryConflictMemberRecord.assertion_id)
        .join(
            MemoryConflictSetRecord,
            MemoryConflictSetRecord.id == MemoryConflictMemberRecord.conflict_set_id,
        )
        .where(
            MemoryConflictSetRecord.lifecycle_state == "open",
            MemoryConflictMemberRecord.assertion_id.in_(assertion_ids or [""]),
        )
        .order_by(MemoryConflictMemberRecord.conflict_set_id.asc())
    ).all():
        open_conflict_ids_by_assertion_id.setdefault(member_assertion_id, []).append(conflict_id)
    salience_by_assertion_id = {
        row.assertion_id: row
        for row in db.scalars(
            select(MemorySalienceRecord).where(
                MemorySalienceRecord.assertion_id.in_(assertion_ids or [""])
            )
        ).all()
    }
    evidence_refs = _evidence_refs_by_assertion(db, assertion_ids)
    topic_membership_by_id = {
        canonical_id: sorted(topic_ids)
        for canonical_id, topic_ids in _topic_membership_for_ids(db, fused_ids).items()
    }

    # (d) Apply the deterministic rails to the fused list. Each excluded candidate
    # is recorded with a reason; survivors keep their fused score and signal ranks.
    survivors: list[tuple[tuple[str, str], float, dict[str, int]]] = []
    rails_excluded: list[dict[str, Any]] = []

    def _exclude(ref: tuple[str, str], kind: str, reason: str) -> None:
        rails_excluded.append({"id": ref[1], "kind": kind, "table": ref[0], "reason": reason})

    for ref, score, ranks in fused:
        table, canonical_id = ref
        if table == _TABLE_ASSERTIONS:
            assertion = assertions_by_id.get(canonical_id)
            kind = (
                "negative_memory"
                if assertion is not None and assertion.assertion_type == "negative"
                else "semantic_assertion"
            )
            if assertion is None or assertion.lifecycle_state != "active":
                _exclude(ref, kind, "lifecycle: assertion is not active")
                continue
            if scope_aliases and not (
                assertion.scope_key in scope_aliases or assertion.subject_key in scope_aliases
            ):
                _exclude(ref, kind, "scope: assertion outside the requested scope")
                continue
            if canonical_id in secret_labelled_ids:
                _exclude(ref, kind, "sensitivity: restricted label excludes this memory")
                continue
            refs = evidence_refs.get(canonical_id, [])
            if any(
                isinstance(item.get("trust_boundary"), str) and item["trust_boundary"] == "tainted"
                for item in refs
            ):
                _exclude(ref, kind, "trust boundary: tainted evidence")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_EPISODES:
            episode = episodes_by_id.get(canonical_id)
            if episode is None or episode.lifecycle_state != "active":
                _exclude(ref, "episode", "lifecycle: episode is not active")
                continue
            if (
                current_session_id is not None
                and episode.scope_key == f"session:{current_session_id}"
            ):
                _exclude(ref, "episode", "scope: current-session episode")
                continue
            if scope_aliases and episode.scope_key not in scope_aliases:
                _exclude(ref, "episode", "scope: episode outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_REASONING_TRACES:
            trace = reasoning_traces_by_id.get(canonical_id)
            if trace is None or trace.lifecycle_state != "active":
                _exclude(ref, "reasoning_trace", "lifecycle: reasoning trace is not active")
                continue
            if (
                current_session_id is not None
                and trace.scope_key == f"session:{current_session_id}"
            ):
                _exclude(ref, "reasoning_trace", "scope: current-session reasoning trace")
                continue
            if scope_aliases and trace.scope_key not in scope_aliases:
                _exclude(ref, "reasoning_trace", "scope: trace outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_ACTION_TRACES:
            action_trace = action_traces_by_id.get(canonical_id)
            if action_trace is None or action_trace.lifecycle_state != "active":
                _exclude(ref, "action_trace", "lifecycle: action trace is not active")
                continue
            if (
                current_session_id is not None
                and action_trace.scope_key == f"session:{current_session_id}"
            ):
                _exclude(ref, "action_trace", "scope: current-session action trace")
                continue
            if scope_aliases and action_trace.scope_key not in scope_aliases:
                _exclude(ref, "action_trace", "scope: action trace outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_PROCEDURES:
            procedure = procedures_by_id.get(canonical_id)
            if procedure is None or procedure.lifecycle_state != "active":
                _exclude(ref, "procedure", "lifecycle: procedure is not active")
                continue
            if procedure.review_state not in ("approved", "auto_approved"):
                _exclude(ref, "procedure", "review: procedure is not approved")
                continue
            if scope_aliases and procedure.scope_key not in scope_aliases:
                _exclude(ref, "procedure", "scope: procedure outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_SNAPSHOTS:
            snapshot = snapshots_by_id.get(canonical_id)
            if snapshot is None or snapshot.lifecycle_state != "active":
                _exclude(ref, "project_state", "lifecycle: project state is not active")
                continue
            if scope_aliases and not (
                snapshot.project_key in scope_aliases
                or f"project:{snapshot.project_key}" in scope_aliases
            ):
                _exclude(ref, "project_state", "scope: project state outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_CONTEXT_BLOCKS:
            block = context_blocks_by_id.get(canonical_id)
            kind = "topic" if block is not None and block.block_type == "topic" else "hot_index"
            if block is None or block.lifecycle_state != "active":
                _exclude(ref, kind, "lifecycle: context block is not active")
                continue
            if scope_aliases and block.scope_key not in scope_aliases:
                _exclude(ref, kind, "scope: context block outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue
        if table == _TABLE_CONFLICT_SETS:
            conflict = conflict_sets_by_id.get(canonical_id)
            if conflict is None or conflict.lifecycle_state != "open":
                _exclude(ref, "conflict", "lifecycle: conflict set is not open")
                continue
            if scope_aliases and conflict.scope_key not in scope_aliases:
                _exclude(ref, "conflict", "scope: conflict set outside the requested scope")
                continue
            survivors.append((ref, score, ranks))
            continue

    # (e+f) Build the per-candidate feature vector and (f) cap the pool to the
    # candidate budget by fused score (survivors are already score-desc, ref-asc).
    candidate_payloads: list[dict[str, Any]] = []
    for ref, score, ranks in survivors[:candidate_limit]:
        table, canonical_id = ref
        topic_membership = topic_membership_by_id.get(canonical_id, [])
        if table == _TABLE_ASSERTIONS:
            assertion = assertions_by_id[canonical_id]
            kind = (
                "negative_memory"
                if assertion.assertion_type == "negative"
                else ("semantic_assertion")
            )
            refs = evidence_refs.get(canonical_id, [])
            payload = serialize_assertion(assertion, evidence_refs=refs)
            payload["kind"] = kind
            payload["lifecycle_state"] = assertion.lifecycle_state
            payload["trust_boundary"] = (
                refs[0]["trust_boundary"]
                if refs and isinstance(refs[0].get("trust_boundary"), str)
                else "reviewed_memory"
            )
            payload["taint"] = {
                "provenance_status": payload["trust_boundary"],
                "evidence_ids": [
                    item["evidence_id"] for item in refs if isinstance(item.get("evidence_id"), str)
                ],
            }
            conflict_ids = open_conflict_ids_by_assertion_id.get(canonical_id, [])
            conflict_status = (
                {"state": "open", "conflict_ids": sorted(conflict_ids)}
                if conflict_ids
                else {"state": "none", "conflict_ids": []}
            )
            payload["conflict_status"] = conflict_status
            salience = salience_by_assertion_id.get(canonical_id)
            payload["retrieval_features"] = {
                "rrf_score": score,
                "signal_ranks": dict(sorted(ranks.items())),
                "vector_distance": vector_distance_by_id.get(canonical_id),
                "lexical_rank": lexical_rank_by_id.get(canonical_id),
                "salience_score": salience.score if salience is not None else None,
                "user_priority": salience.user_priority if salience is not None else None,
                "source_trust": payload["trust_boundary"],
                "effective_confidence": _effective_confidence(assertion, now=now),
                "verification_age_days": max(
                    0.0, (now - assertion.last_verified_at).total_seconds() / 86_400.0
                ),
                "conflict_status": conflict_status,
                "validity": {
                    "valid_from": to_rfc3339(assertion.valid_from)
                    if assertion.valid_from
                    else None,
                    "valid_to": to_rfc3339(assertion.valid_to) if assertion.valid_to else None,
                },
                "topic_membership": topic_membership,
                "entity_ids": sorted(entity_ids_by_id.get(canonical_id, [])),
                "symbol_matches": symbol_features_by_id.get(canonical_id, []),
            }
            candidate_payloads.append(payload)
            continue
        if table == _TABLE_EPISODES:
            episode = episodes_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": episode.id,
                    "kind": "episode",
                    "episode_type": episode.episode_type,
                    "scope_key": episode.scope_key,
                    "summary": redact_text(episode.summary),
                    "outcome": redact_text(episode.outcome) if episode.outcome else None,
                    "primary_evidence_id": episode.primary_evidence_id,
                    "lifecycle_state": episode.lifecycle_state,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "occurred_at": to_rfc3339(episode.occurred_at),
                }
            )
            continue
        if table == _TABLE_REASONING_TRACES:
            trace = reasoning_traces_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": trace.id,
                    "kind": "reasoning_trace",
                    "scope_key": trace.scope_key,
                    "trace_type": trace.trace_type,
                    "task_summary": redact_text(trace.task_summary),
                    "trace_summary": redact_text(trace.trace_summary),
                    "outcome": trace.outcome,
                    "primary_evidence_id": trace.primary_evidence_id,
                    "source_turn_id": trace.source_turn_id,
                    "lifecycle_state": trace.lifecycle_state,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "updated_at": to_rfc3339(trace.updated_at),
                }
            )
            continue
        if table == _TABLE_ACTION_TRACES:
            action_trace = action_traces_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": action_trace.id,
                    "kind": "action_trace",
                    "scope_key": action_trace.scope_key,
                    "trace_type": action_trace.trace_type,
                    "action_attempt_id": action_trace.action_attempt_id,
                    "source_turn_id": action_trace.source_turn_id,
                    "capability_id": action_trace.capability_id,
                    "summary": redact_text(action_trace.summary),
                    "outcome": action_trace.outcome,
                    "primary_evidence_id": action_trace.primary_evidence_id,
                    "lifecycle_state": action_trace.lifecycle_state,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "updated_at": to_rfc3339(action_trace.updated_at),
                }
            )
            continue
        if table == _TABLE_PROCEDURES:
            procedure = procedures_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": procedure.id,
                    "kind": "procedure",
                    "procedure_key": procedure.procedure_key,
                    "scope_key": procedure.scope_key,
                    "instruction": redact_text(procedure.instruction),
                    "source_assertion_id": procedure.source_assertion_id,
                    "lifecycle_state": procedure.lifecycle_state,
                    "review_state": procedure.review_state,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "updated_at": to_rfc3339(procedure.updated_at),
                }
            )
            continue
        if table == _TABLE_SNAPSHOTS:
            snapshot = snapshots_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": snapshot.id,
                    "kind": "project_state",
                    "project_key": snapshot.project_key,
                    "summary": redact_text(snapshot.summary),
                    "state": _redact_json_value(snapshot.state),
                    "lifecycle_state": snapshot.lifecycle_state,
                    "source_assertion_ids": snapshot.source_assertion_ids,
                    "source_evidence_ids": snapshot.source_evidence_ids,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "updated_at": to_rfc3339(snapshot.updated_at),
                }
            )
            continue
        if table == _TABLE_CONTEXT_BLOCKS:
            block = context_blocks_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": block.id,
                    "kind": "topic" if block.block_type == "topic" else "hot_index",
                    "topic_id": block.topic_id,
                    "scope_key": block.scope_key,
                    "content": redact_text(block.content),
                    "source_assertion_ids": block.source_assertion_ids,
                    "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                    "lifecycle_state": block.lifecycle_state,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "none", "conflict_ids": []},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": block.projection_version,
                    "updated_at": to_rfc3339(block.updated_at),
                }
            )
            continue
        if table == _TABLE_CONFLICT_SETS:
            conflict = conflict_sets_by_id[canonical_id]
            candidate_payloads.append(
                {
                    "id": conflict.id,
                    "kind": "conflict",
                    "scope_key": conflict.scope_key,
                    "predicate": conflict.predicate,
                    "conflict_type": conflict.conflict_type,
                    "lifecycle_state": conflict.lifecycle_state,
                    "resolution_assertion_id": conflict.resolution_assertion_id,
                    "trust_boundary": "reviewed_memory",
                    "taint": {"provenance_status": "reviewed_memory"},
                    "conflict_status": {"state": "open", "conflict_ids": [conflict.id]},
                    "retrieval_features": _non_assertion_features(
                        score, ranks, lexical_rank_by_id.get(canonical_id), topic_membership
                    ),
                    "projection_version": MEMORY_PROJECTION_VERSION,
                    "updated_at": to_rfc3339(conflict.updated_at),
                }
            )
            continue

    # Recent conversational turns are context for the curator's judgement.
    recent_turns = list(
        reversed(
            db.scalars(
                select(TurnRecord)
                .order_by(TurnRecord.created_at.desc(), TurnRecord.id.desc())
                .limit(8)
            ).all()
        )
    )
    history = [
        {
            "turn_id": turn.id,
            "user_message": _clean_text(turn.user_message, max_chars=500),
            "assistant_message": _clean_text(turn.assistant_message or "", max_chars=500),
            "status": turn.status,
        }
        for turn in recent_turns
    ]

    # AI curation owns relevance: the deterministic pool is handed over whole and
    # the curator must account for every candidate.
    if candidate_payloads:
        curation = _curate_memory_context_with_model(
            user_message=user_message,
            history=history,
            candidates=candidate_payloads,
            max_selected=max_recalled_assertions,
            settings=resolved_settings,
        )
    else:
        curation = {
            "selected_memories": [],
            "omitted_memories": [],
            "rationale": "No memory candidates were available for AI curation.",
            "uncertainty": "",
            "confidence": 1.0,
            "model": None,
            "prompt_version": MEMORY_CURATION_PROMPT_VERSION,
            "parse_status": "not_required_no_candidates",
        }

    selected_ids = [item["id"] for item in curation["selected_memories"]]
    selected_by_kind: dict[str, list[str]] = {}
    for item in curation["selected_memories"]:
        selected_by_kind.setdefault(item["kind"], []).append(item["id"])
    candidate_payloads_by_id = {item["id"]: item for item in candidate_payloads}

    selected_assertions = [
        assertions_by_id[memory_id]
        for memory_id in selected_by_kind.get("semantic_assertion", [])
        if memory_id in assertions_by_id
    ]
    semantic_assertions = []
    for assertion in selected_assertions:
        serialized = serialize_assertion(
            assertion, evidence_refs=evidence_refs.get(assertion.id, [])
        )
        payload = candidate_payloads_by_id.get(assertion.id, {})
        serialized["conflict_status"] = payload.get(
            "conflict_status", {"state": "none", "conflict_ids": []}
        )
        serialized["retrieval_features"] = payload.get("retrieval_features", {})
        semantic_assertions.append(serialized)
    selected_negatives = [
        assertions_by_id[memory_id]
        for memory_id in selected_by_kind.get("negative_memory", [])
        if memory_id in assertions_by_id
    ]
    negative_memory = []
    for assertion in selected_negatives:
        serialized = serialize_assertion(
            assertion, evidence_refs=evidence_refs.get(assertion.id, [])
        )
        payload = candidate_payloads_by_id.get(assertion.id, {})
        serialized["conflict_status"] = payload.get(
            "conflict_status", {"state": "none", "conflict_ids": []}
        )
        serialized["retrieval_features"] = payload.get("retrieval_features", {})
        negative_memory.append(serialized)

    selected_episodes = [
        episodes_by_id[memory_id]
        for memory_id in selected_by_kind.get("episode", [])
        if memory_id in episodes_by_id
    ]
    selected_reasoning_traces = [
        reasoning_traces_by_id[memory_id]
        for memory_id in selected_by_kind.get("reasoning_trace", [])
        if memory_id in reasoning_traces_by_id
    ]
    selected_action_traces = [
        action_traces_by_id[memory_id]
        for memory_id in selected_by_kind.get("action_trace", [])
        if memory_id in action_traces_by_id
    ]
    selected_procedures = [
        procedures_by_id[memory_id]
        for memory_id in selected_by_kind.get("procedure", [])
        if memory_id in procedures_by_id
    ]
    selected_snapshots = [
        snapshots_by_id[memory_id]
        for memory_id in selected_by_kind.get("project_state", [])
        if memory_id in snapshots_by_id
    ]
    selected_hot_blocks = [
        context_blocks_by_id[memory_id]
        for memory_id in selected_by_kind.get("hot_index", [])
        if memory_id in context_blocks_by_id
    ]
    selected_topic_blocks = [
        context_blocks_by_id[memory_id]
        for memory_id in selected_by_kind.get("topic", [])
        if memory_id in context_blocks_by_id
    ]

    # Open conflict sets are surfaced whole, independent of curation: a
    # contradiction is always presented as uncertainty until it is resolved.
    conflicts = db.scalars(
        select(MemoryConflictSetRecord)
        .where(MemoryConflictSetRecord.lifecycle_state == "open")
        .order_by(MemoryConflictSetRecord.updated_at.desc(), MemoryConflictSetRecord.id.asc())
    ).all()

    recall_window: dict[str, Any] = {
        "max_selected_memories": max_recalled_assertions,
        "selected_memory_count": len(curation["selected_memories"]),
        "memory_candidate_count": len(candidate_payloads),
        "omitted_memory_count": len(curation["omitted_memories"]),
        "rails_excluded_count": len(rails_excluded),
        "selected_memory_ids": selected_ids,
        "selected_memories": list(curation["selected_memories"]),
        "omitted_memories": list(curation["omitted_memories"]),
        "candidate_memory_ids": [item["id"] for item in candidate_payloads],
        "candidate_memories": candidate_payloads,
        "rails_excluded": rails_excluded,
        "curation_rationale": curation["rationale"],
        "curation_uncertainty": curation["uncertainty"],
        "curation_confidence": curation["confidence"],
        "curation_model": curation["model"],
        "curation_prompt_version": curation["prompt_version"],
        "curation_parse_status": curation["parse_status"],
        "curation_provider_response_id": curation.get("provider_response_id"),
    }
    context = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        "hot_index": [
            {
                "id": block.id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in selected_hot_blocks
        ],
        "topic_index": [
            {
                "id": block.id,
                "topic_id": block.topic_id,
                "scope_key": block.scope_key,
                "content": redact_text(block.content),
                "source_assertion_ids": block.source_assertion_ids,
                "source_project_state_snapshot_ids": block.source_project_state_snapshot_ids,
                "projection_version": block.projection_version,
                "updated_at": to_rfc3339(block.updated_at),
            }
            for block in selected_topic_blocks
        ],
        "pinned_core": [
            item for item in semantic_assertions if item["type"] in {"profile", "preference"}
        ],
        "project_state": [
            {
                "id": snapshot.id,
                "project_key": snapshot.project_key,
                "summary": redact_text(snapshot.summary),
                "state": _redact_json_value(snapshot.state),
                "source_assertion_ids": snapshot.source_assertion_ids,
                "source_evidence_ids": snapshot.source_evidence_ids,
                "created_at": to_rfc3339(snapshot.created_at),
                "updated_at": to_rfc3339(snapshot.updated_at),
            }
            for snapshot in selected_snapshots
        ],
        "commitments_and_decisions": [
            item for item in semantic_assertions if item["type"] in {"commitment", "decision"}
        ][:12],
        "semantic_assertions": semantic_assertions,
        "episodic_evidence": [
            {
                "id": episode.id,
                "type": episode.episode_type,
                "scope_key": episode.scope_key,
                "summary": redact_text(episode.summary),
                "outcome": redact_text(episode.outcome) if episode.outcome else None,
                "primary_evidence_id": episode.primary_evidence_id,
                "occurred_at": to_rfc3339(episode.occurred_at),
            }
            for episode in selected_episodes
        ],
        "procedural_memory": [
            {
                "id": procedure.id,
                "procedure_key": procedure.procedure_key,
                "scope_key": procedure.scope_key,
                "instruction": redact_text(procedure.instruction),
                "source_assertion_id": procedure.source_assertion_id,
            }
            for procedure in selected_procedures
        ],
        "action_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "action_attempt_id": trace.action_attempt_id,
                "source_turn_id": trace.source_turn_id,
                "capability_id": trace.capability_id,
                "summary": redact_text(trace.summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in selected_action_traces
        ],
        "reasoning_traces": [
            {
                "id": trace.id,
                "scope_key": trace.scope_key,
                "trace_type": trace.trace_type,
                "task_summary": redact_text(trace.task_summary),
                "trace_summary": redact_text(trace.trace_summary),
                "outcome": trace.outcome,
                "primary_evidence_id": trace.primary_evidence_id,
                "source_turn_id": trace.source_turn_id,
                "updated_at": to_rfc3339(trace.updated_at),
            }
            for trace in selected_reasoning_traces
        ],
        "negative_memory": negative_memory,
        "conflicts": [_serialize_conflict(conflict) for conflict in conflicts],
        "recall_window": recall_window,
        "memory_policy": memory_policy,
        "projection_health": {
            "projection_version": MEMORY_PROJECTION_VERSION,
            "selected_assertion_count": len(selected_assertions),
            "selected_memory_count": len(curation["selected_memories"]),
            **_projection_health(db),
        },
    }
    event_payload = {
        "schema_version": MEMORY_CONTEXT_SCHEMA_VERSION,
        "projection_version": MEMORY_PROJECTION_VERSION,
        **recall_window,
        "conflict_ids": [conflict.id for conflict in conflicts],
        "memory_policy": memory_policy,
    }
    return context, event_payload


def context_text(memory_context: dict[str, Any]) -> str:
    lines = ["memory context:"]
    for item in memory_context.get("hot_index", []):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            lines.append("- hot: " + item["content"])
    for item in memory_context.get("topic_index", []):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            lines.append("- topic: " + item["content"])
    for item in memory_context.get("project_state", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- project: " + item["summary"])
    for item in memory_context.get("commitments_and_decisions", []):
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            conflict_status = item.get("conflict_status")
            if isinstance(conflict_status, dict) and conflict_status.get("state") == "open":
                lines.append("- conflicted commitment/decision: " + item["value"])
                continue
            lines.append("- commitment/decision: " + item["value"])
    for item in memory_context.get("semantic_assertions", []):
        if not isinstance(item, dict):
            continue
        memory_type = item.get("type")
        subject_key = item.get("subject_key")
        predicate = item.get("predicate")
        value = item.get("value")
        if all(isinstance(part, str) for part in (memory_type, subject_key, predicate, value)):
            conflict_status = item.get("conflict_status")
            if isinstance(conflict_status, dict) and conflict_status.get("state") == "open":
                conflict_ids = conflict_status.get("conflict_ids")
                conflict_suffix = (
                    f" conflicts={','.join(conflict_ids)}"
                    if isinstance(conflict_ids, list)
                    and all(isinstance(conflict_id, str) for conflict_id in conflict_ids)
                    else ""
                )
                lines.append(
                    f"- conflict: {memory_type}: {subject_key} {predicate} = {value}"
                    f"{conflict_suffix}"
                )
                continue
            lines.append(f"- {memory_type}: {subject_key} {predicate} = {value}")
            evidence_refs = item.get("evidence_refs")
            if isinstance(evidence_refs, list) and evidence_refs:
                first_ref = evidence_refs[0]
                if isinstance(first_ref, dict) and isinstance(first_ref.get("snippet"), str):
                    lines.append(f"  evidence: {first_ref['snippet']}")
    for item in memory_context.get("episodic_evidence", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- episode: " + item["summary"])
    for item in memory_context.get("procedural_memory", []):
        if isinstance(item, dict) and isinstance(item.get("instruction"), str):
            lines.append("- procedure: " + item["instruction"])
    for item in memory_context.get("action_traces", []):
        if isinstance(item, dict) and isinstance(item.get("summary"), str):
            lines.append("- action: " + item["summary"])
    for item in memory_context.get("reasoning_traces", []):
        if isinstance(item, dict) and isinstance(item.get("trace_summary"), str):
            trace_type = item.get("trace_type")
            prefix = f"reasoning ({trace_type})" if isinstance(trace_type, str) else "reasoning"
            lines.append(f"- {prefix}: " + item["trace_summary"])
    negative_memory = memory_context.get("negative_memory")
    if isinstance(negative_memory, list) and negative_memory:
        lines.append("do not repeat:")
        for item in negative_memory:
            if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                continue
            conflict_status = item.get("conflict_status")
            if isinstance(conflict_status, dict) and conflict_status.get("state") == "open":
                lines.append("- conflicted negative memory: " + item["value"])
                continue
            lines.append("- " + item["value"])
    conflicts = memory_context.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        lines.append("- unresolved memory conflicts exist; state uncertainty when relevant")
    return "\n".join(lines)


def _extract_output_text(output_items: Any) -> str:
    if not isinstance(output_items, list):
        return ""
    parts: list[str] = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict) and content_item.get("type") == "output_text":
                text = content_item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()


def process_memory_extract_turn(
    *,
    session_factory: sessionmaker[Session],
    task_payload: dict[str, Any],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> None:
    evidence_id = task_payload.get("evidence_id")
    session_id = task_payload.get("session_id")
    if not isinstance(evidence_id, str) or not isinstance(session_id, str):
        raise RuntimeError("memory extraction task payload is malformed")

    with session_factory() as db:
        with db.begin():
            evidence = db.get(MemoryEvidenceRecord, evidence_id)
            if evidence is None or evidence.lifecycle_state != "available":
                return
            project_key, repo_key = scope_keys_for_policy(
                _matching_scope_key_for_text(db, text=evidence.source_text)
            )
            if not resolve_memory_policy(
                db,
                operation="extract",
                now=now_fn(),
                session_id=session_id,
                project_key=project_key,
                repo_key=repo_key,
            ).allowed:
                return
            if (
                _never_remember_rule_for_text(
                    db,
                    source_session_id=session_id,
                    scope_key="global",
                    text=evidence.source_text,
                )
                is not None
            ):
                return
            source_text = evidence.source_text

    if not settings.openai_api_key:
        raise RuntimeError("memory extraction requires ARIEL_OPENAI_API_KEY")

    prompt = (
        "Extract durable Ariel memory from the evidence. Return JSON only with two "
        "top-level arrays: candidates and reasoning_traces. "
        "Each candidate must have subject_key, predicate, assertion_type, value, "
        "confidence. Use assertion_type values fact, profile, preference, commitment, "
        "decision, project_state, procedure, domain_concept, or negative. Use the "
        "negative assertion_type for knowledge about what NOT to do — rejected "
        "approaches, invalid assumptions, areas already checked, or unsafe operations "
        "— with a negative.* predicate (negative.rejected_approach, "
        "negative.invalid_assumption, negative.already_checked, "
        "negative.unsafe_operation, negative.known_bad_path). "
        "Each reasoning_trace must have trace_type, task_summary, trace_summary, "
        "outcome. Use trace_type values action_path, failure, user_correction, "
        "successful_pattern, or diagnostic, and outcome values succeeded, failed, "
        "corrected, or unknown. Return empty arrays when the evidence has no durable "
        "memory."
    )
    provider_response_id: str | None = None
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={
                "authorization": f"Bearer {settings.openai_api_key}",
                "content-type": "application/json",
            },
            json={
                "model": settings.model_name,
                "input": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": source_text},
                ],
                "store": False,
                "text": {"verbosity": "low"},
            },
            timeout=settings.model_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=None,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_AI_JUDGMENT_REQUIRED",
                        failure_reason=_clean_text(str(exc), max_chars=500),
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError("memory extraction model request failed") from exc
    if response.status_code >= 400:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=None,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={"status_code": response.status_code},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="missing_output",
                        validation_status="not_validated",
                        failure_code="E_AI_JUDGMENT_REQUIRED",
                        failure_reason=f"HTTP {response.status_code}",
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError(f"memory extraction model returned HTTP {response.status_code}")
    response_payload = response.json()
    raw_provider_response_id = (
        response_payload.get("id") if isinstance(response_payload, dict) else None
    )
    provider_response_id = (
        raw_provider_response_id if isinstance(raw_provider_response_id, str) else None
    )
    text = _extract_output_text(
        response_payload.get("output") if isinstance(response_payload, dict) else None
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=provider_response_id,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="invalid_json",
                        validation_status="invalid",
                        failure_code="E_AI_JUDGMENT_INVALID_JSON",
                        failure_reason="memory extraction model returned malformed JSON",
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError("memory extraction model returned malformed JSON") from exc
    candidates = payload.get("candidates")
    reasoning_traces = payload.get("reasoning_traces", [])
    schema_error = None
    if not isinstance(candidates, list):
        schema_error = "memory extraction JSON missing candidates array"
    elif len(candidates) > 8:
        schema_error = "memory extraction JSON returned too many candidates"
    elif not isinstance(reasoning_traces, list):
        schema_error = "memory extraction JSON reasoning_traces must be an array"
    elif len(reasoning_traces) > 8:
        schema_error = "memory extraction JSON returned too many reasoning traces"
    else:
        for raw_candidate in candidates:
            if not isinstance(raw_candidate, dict) or set(raw_candidate.keys()) != {
                "subject_key",
                "predicate",
                "assertion_type",
                "value",
                "confidence",
            }:
                schema_error = "memory extraction candidate schema invalid"
                break
            confidence = raw_candidate.get("confidence")
            if (
                not isinstance(raw_candidate.get("subject_key"), str)
                or not raw_candidate["subject_key"].strip()
                or not isinstance(raw_candidate.get("predicate"), str)
                or not raw_candidate["predicate"].strip()
                or raw_candidate.get("assertion_type") not in ALLOWED_MEMORY_ASSERTION_TYPES
                or not isinstance(raw_candidate.get("value"), str)
                or not raw_candidate["value"].strip()
                or isinstance(confidence, bool)
                or not isinstance(confidence, int | float)
                or float(confidence) < 0.0
                or float(confidence) > 1.0
                or float(confidence) != float(confidence)
            ):
                schema_error = "memory extraction candidate schema invalid"
                break
        for raw_trace in reasoning_traces:
            if (
                not isinstance(raw_trace, dict)
                or set(raw_trace.keys())
                != {"trace_type", "task_summary", "trace_summary", "outcome"}
                or raw_trace.get("trace_type") not in ALLOWED_MEMORY_REASONING_TRACE_TYPES
                or raw_trace.get("outcome") not in ALLOWED_MEMORY_REASONING_TRACE_OUTCOMES
                or not isinstance(raw_trace.get("task_summary"), str)
                or not raw_trace["task_summary"].strip()
                or not isinstance(raw_trace.get("trace_summary"), str)
                or not raw_trace["trace_summary"].strip()
            ):
                schema_error = "memory extraction reasoning trace schema invalid"
                break
    if schema_error is not None:
        with session_factory() as db:
            with db.begin():
                now = now_fn()
                db.add(
                    AIJudgmentRecord(
                        id=new_id_fn("ajg"),
                        judgment_type="memory_extraction",
                        source_type="memory_evidence",
                        source_id=evidence_id,
                        status="failed",
                        model=settings.model_name,
                        prompt_version="memory-extraction-v1",
                        provider_response_id=provider_response_id,
                        input_summary="memory extraction for turn evidence",
                        input_refs={"session_id": session_id, "evidence_id": evidence_id},
                        selected=[],
                        omitted=[],
                        output={},
                        rationale=None,
                        uncertainty=None,
                        confidence=None,
                        parse_status="schema_invalid",
                        validation_status="invalid",
                        failure_code="E_AI_JUDGMENT_SCHEMA",
                        failure_reason=schema_error,
                        created_at=now,
                        updated_at=now,
                    )
                )
        raise RuntimeError(schema_error)

    with session_factory() as db:
        with db.begin():
            evidence = db.get(MemoryEvidenceRecord, evidence_id)
            if evidence is None or evidence.lifecycle_state != "available":
                return
            project_key, repo_key = scope_keys_for_policy(
                _matching_scope_key_for_text(db, text=evidence.source_text)
            )
            if not resolve_memory_policy(
                db,
                operation="extract",
                now=now_fn(),
                session_id=session_id,
                project_key=project_key,
                repo_key=repo_key,
            ).allowed:
                return
            proposed_candidate_ids: list[str] = []
            for raw_candidate in candidates:
                subject_key = raw_candidate.get("subject_key")
                predicate = raw_candidate.get("predicate")
                assertion_type = raw_candidate.get("assertion_type")
                value = raw_candidate.get("value")
                confidence = raw_candidate.get("confidence")
                memory_events = propose_memory_candidate(
                    db,
                    source_session_id=session_id,
                    actor_id="system",
                    evidence_text=source_text,
                    subject_key=subject_key,
                    predicate=predicate,
                    assertion_type=assertion_type,
                    value=value,
                    confidence=float(confidence),
                    scope_key="global",
                    valid_from=None,
                    valid_to=None,
                    extraction_model=settings.model_name,
                    extraction_prompt_version="memory-extraction-v1",
                    now_fn=now_fn,
                    new_id_fn=new_id_fn,
                    source_evidence_id=evidence_id,
                )
                for event in memory_events:
                    payload_data = event.get("payload")
                    if (
                        event.get("event_type") == "evt.memory.candidate_proposed"
                        and isinstance(payload_data, dict)
                        and isinstance(payload_data.get("assertion_id"), str)
                    ):
                        proposed_candidate_ids.append(payload_data["assertion_id"])
                emit_memory_events(
                    db,
                    events=memory_events,
                    entry_path="worker",
                    actor_id="system",
                    scope_key=f"session:{session_id}",
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                )
            proposed_trace_ids: list[str] = []
            for raw_trace in reasoning_traces:
                trace, trace_events = record_reasoning_trace(
                    db,
                    scope_key=f"session:{session_id}",
                    trace_type=raw_trace["trace_type"],
                    task_summary=raw_trace["task_summary"],
                    trace_summary=raw_trace["trace_summary"],
                    outcome=raw_trace["outcome"],
                    primary_evidence_id=evidence_id,
                    source_turn_id=evidence.source_turn_id,
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                )
                proposed_trace_ids.append(trace.id)
                emit_memory_events(
                    db,
                    events=trace_events,
                    entry_path="worker",
                    actor_id="system",
                    scope_key=f"session:{session_id}",
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                )
            now = now_fn()
            db.add(
                AIJudgmentRecord(
                    id=new_id_fn("ajg"),
                    judgment_type="memory_extraction",
                    source_type="memory_evidence",
                    source_id=evidence_id,
                    status="succeeded",
                    model=settings.model_name,
                    prompt_version="memory-extraction-v1",
                    provider_response_id=provider_response_id,
                    input_summary="memory extraction for turn evidence",
                    input_refs={"session_id": session_id, "evidence_id": evidence_id},
                    selected=[
                        {"assertion_id": assertion_id} for assertion_id in proposed_candidate_ids
                    ]
                    + [{"reasoning_trace_id": trace_id} for trace_id in proposed_trace_ids],
                    omitted=[],
                    output={
                        "candidate_count": len(proposed_candidate_ids),
                        "reasoning_trace_count": len(proposed_trace_ids),
                    },
                    rationale="extracted durable memory candidates from evidence",
                    uncertainty=None,
                    confidence=None,
                    parse_status="parsed",
                    validation_status="valid",
                    failure_code=None,
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
