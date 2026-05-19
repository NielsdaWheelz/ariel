"""Ariel's memory subsystem: a flat fact store and two AI-maintained documents.

Memory has three parts, all authored by AI:

- the **fact store** -- durable plain-language facts, one flat ``memory_facts``
  table with no categories;
- the **profile** -- one always-loaded ``memory_profile`` document: who the user
  is, how they work, the standing context, and privacy guardrails;
- the **session digest** -- the working state of one conversation, held on
  ``sessions.digest``.

Two bounded AI subagents own every memory judgment. The **retriever**
(:func:`run_retriever`) decides which facts matter for a wake; the
**rememberer** (:func:`run_rememberer`) decides what to write to the store and
keeps the profile and digest current. Each is one stateless, audited
``httpx`` call to the OpenAI Responses API, shaped exactly like every other
bounded AI call in the codebase.

Deterministic code here does five things, all rails: it stores facts and the
two documents, it gathers candidate facts (:func:`gather_candidates`), it
renders the profile and recalled facts into turn context, it runs the
subagents, and it writes one ``ai_judgments`` row per subagent call. It makes
no relevance, importance, categorization, conflict, ranking, or
"worth remembering" decision, and it summarizes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any, Literal

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .config import AppSettings
from .persistence import (
    AIJudgmentRecord,
    BackgroundTaskRecord,
    MemoryFactRecord,
    MemoryProfileRecord,
    SessionRecord,
    TurnRecord,
    enqueue_background_task,
)
from .redaction import redact_text


# ---------------------------------------------------------------------------
# Prompt versions
#
# Each subagent's prompt is a first-class, versioned artifact. The version
# constant is embedded in the user-message JSON of every call: it audits which
# prompt produced a judgment, and a caller (or a test fake) can branch on it to
# tell a retriever call from a rememberer call without parsing prose.
# ---------------------------------------------------------------------------
RETRIEVER_PROMPT_VERSION = "memory-retriever-v1"
REMEMBERER_PROMPT_VERSION = "memory-rememberer-v1"

# How long a row may sit ``forgotten`` before the sweep hard-deletes it.
_FORGOTTEN_RETENTION = timedelta(days=30)

# A short evidence snippet stored on a fact's ``source_excerpt`` is capped here.
_EXCERPT_MAX_CHARS = 700

_RETRIEVER_PROMPT = (
    "You are Ariel's memory retriever. You are given the current wake context "
    "(a user message or a proactive case summary) and a candidate set of stored "
    "facts, each with an id and plain-language content. The candidates are an "
    "unranked union of vector, keyword, and recency matches -- their order is "
    "transport, not relevance. Decide which facts genuinely matter for this "
    "wake and would help Ariel respond well. Be selective: surface a fact only "
    "when it is actually pertinent, and omit the rest. Return JSON only, with a "
    'single key "facts" -- an array of the ids of the facts that matter now. '
    "Return an empty array when none of the candidates are relevant. Never "
    "invent an id; every id you return must be one of the candidates."
)

_REMEMBERER_PROMPT = (
    "You are Ariel's memory rememberer. You maintain three things: the flat "
    "fact store, the always-loaded user profile, and the current session "
    "digest.\n\n"
    "You are given the current profile, the current session digest, a set of "
    "existing facts (each with an id and content) so you can edit instead of "
    "duplicate, and the material to review -- a completed conversation, an "
    "on-demand note, or the fact store itself for a periodic sweep.\n\n"
    "Honor the profile's privacy guardrails absolutely: if the profile says "
    "never to remember something, do not store it.\n\n"
    "Decide what should change and return JSON only with three keys:\n"
    '- "operations": an array of fact operations. Each is one of '
    '{"op": "write", "content": "<the new fact, in rich plain language>"}, '
    '{"op": "edit", "fact_id": "<id of an existing fact>", '
    '"content": "<the corrected fact>"}, or '
    '{"op": "forget", "fact_id": "<id of an existing fact>"}. Write a fact '
    "when the material contains durable knowledge worth keeping -- a "
    "preference, a decision, a relationship, a stable piece of context. Edit a "
    "fact when it is now partly wrong or out of date. Forget a fact when it is "
    "wrong, stale, or superseded. Facts are flat plain-language statements: do "
    "not categorize or tag them.\n"
    '- "profile": the full rewritten profile document, or null to leave it '
    "unchanged. Rewrite the profile when something durable about the user "
    "changed. Keep it tight: synthesize, merge, and drop -- it is loaded into "
    "every turn.\n"
    '- "digest": the full rewritten session digest, or null to leave it '
    "unchanged. The digest is the working state of the current conversation -- "
    "the thread of discussion, what has been tried, open questions, where "
    "things stand. Keep it no longer than is useful.\n\n"
    "Return an empty operations array and null profile and digest when nothing "
    "should change."
)


# ---------------------------------------------------------------------------
# Bounded AI-call failure
# ---------------------------------------------------------------------------
class AIJudgmentFailure(RuntimeError):
    """A bounded memory AI call failed -- network, malformed output, or a
    schema/validation violation. Carries the fields an ``ai_judgments`` row
    needs so the caller can audit the failure. Memory's subagents fail closed:
    every malformed model output raises this rather than being partly parsed."""

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


# ---------------------------------------------------------------------------
# Validated subagent outputs
#
# Pure dataclasses. The model returns raw JSON; a ``_validated_*`` function
# narrows it into one of these or raises ``AIJudgmentFailure``. Downstream code
# only ever sees the narrowed type.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FactOperation:
    """One rememberer fact-store mutation. ``content`` is set for write/edit and
    empty for forget; ``fact_id`` is set for edit/forget and empty for write."""

    op: Literal["write", "edit", "forget"]
    content: str
    fact_id: str


@dataclass(frozen=True)
class RemembererOutput:
    """The validated rememberer judgment: fact operations plus optional full
    rewrites of the profile and the session digest. ``None`` means leave that
    document unchanged; a string replaces it wholesale."""

    operations: tuple[FactOperation, ...]
    profile: str | None
    digest: str | None


# ---------------------------------------------------------------------------
# The embedding call
# ---------------------------------------------------------------------------
def embed_text(text: str, *, settings: AppSettings) -> list[float]:
    """Embed ``text`` with the configured OpenAI embedding model. Used to give
    a written or edited fact its ``embedding`` vector, and to embed a retriever
    query for vector candidate gathering."""
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


# ---------------------------------------------------------------------------
# Fact-store and profile reads/writes
# ---------------------------------------------------------------------------
def read_profile(db: Session) -> str:
    """Return the singleton profile document's content. The migration seeds one
    ``memory_profile`` row, so the profile always exists."""
    profile = db.scalars(select(MemoryProfileRecord).limit(1)).first()
    if profile is None:
        raise RuntimeError("memory_profile is empty: the seed row is missing")
    return profile.content


def write_profile(db: Session, *, content: str, now: datetime) -> None:
    """Replace the singleton profile document with ``content``."""
    profile = db.scalars(select(MemoryProfileRecord).limit(1)).first()
    if profile is None:
        raise RuntimeError("memory_profile is empty: the seed row is missing")
    profile.content = content
    profile.updated_at = now


def list_active_facts(db: Session, *, limit: int) -> list[MemoryFactRecord]:
    """The most-recently-updated active facts. Used by the sweep, which reviews
    the store rather than a conversation."""
    return list(
        db.scalars(
            select(MemoryFactRecord)
            .where(MemoryFactRecord.status == "active")
            .order_by(MemoryFactRecord.updated_at.desc(), MemoryFactRecord.id.asc())
            .limit(limit)
        ).all()
    )


def gather_candidates(
    db: Session,
    *,
    query: str,
    settings: AppSettings,
    limit: int,
) -> list[MemoryFactRecord]:
    """The candidate gather: a generous, unranked union of vector-similarity
    matches over ``embedding``, keyword matches over ``search_vector``, and the
    most recent active facts.

    This is a rail, not a ranking. There is no similarity threshold and no
    fusion -- the retriever and the rememberer are meant to see many facts,
    including low-similarity ones, and judge for themselves. Vector and keyword
    only keep the candidate set bounded as the store grows; at prototype scale
    the union is effectively most of the store. Forgotten facts are excluded.
    """
    facts: dict[str, MemoryFactRecord] = {}

    # Keyword: full-text match over the generated search_vector.
    tsquery = func.websearch_to_tsquery("english", query)
    for fact in db.scalars(
        select(MemoryFactRecord)
        .where(
            MemoryFactRecord.status == "active",
            MemoryFactRecord.search_vector.op("@@")(tsquery),
        )
        .order_by(
            func.ts_rank_cd(MemoryFactRecord.search_vector, tsquery).desc(),
            MemoryFactRecord.id.asc(),
        )
        .limit(limit)
    ).all():
        facts[fact.id] = fact

    # Vector: cosine-nearest active facts to the query embedding. Skipped when
    # no fact carries an embedding yet, which keeps a cold store from making a
    # needless embedding call.
    embedded_exists = db.scalar(
        select(MemoryFactRecord.id)
        .where(
            MemoryFactRecord.status == "active",
            MemoryFactRecord.embedding.is_not(None),
        )
        .limit(1)
    )
    if embedded_exists is not None and query.strip():
        distance = MemoryFactRecord.embedding.cosine_distance(embed_text(query, settings=settings))
        for fact in db.scalars(
            select(MemoryFactRecord)
            .where(
                MemoryFactRecord.status == "active",
                MemoryFactRecord.embedding.is_not(None),
            )
            .order_by(distance.asc(), MemoryFactRecord.id.asc())
            .limit(limit)
        ).all():
            facts[fact.id] = fact

    # Recency: the most-recently-updated active facts, so a brand-new fact with
    # no embedding and no keyword overlap is still a candidate.
    for fact in db.scalars(
        select(MemoryFactRecord)
        .where(MemoryFactRecord.status == "active")
        .order_by(MemoryFactRecord.updated_at.desc(), MemoryFactRecord.id.asc())
        .limit(limit)
    ).all():
        facts[fact.id] = fact

    return sorted(facts.values(), key=lambda fact: fact.updated_at, reverse=True)


# ---------------------------------------------------------------------------
# Context rendering
# ---------------------------------------------------------------------------
def render_profile(profile: str) -> str:
    """Render the profile document as a ``system`` message body for a turn or a
    proactive deliberation. Returns an empty string for an empty profile so the
    caller can skip the section."""
    content = profile.strip()
    if not content:
        return ""
    return "user profile (durable knowledge about the user):\n" + content


def render_recalled_facts(facts: Sequence[MemoryFactRecord]) -> str:
    """Render the facts the retriever surfaced as a ``recalled memory``
    ``system`` message body, separate from the profile and the digest. Returns
    an empty string when nothing was recalled."""
    if not facts:
        return ""
    lines = ["recalled memory (facts relevant to this turn):"]
    lines.extend(f"- {fact.content}" for fact in facts)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The Responses API call
# ---------------------------------------------------------------------------
def _extract_output_text(output_items: Any) -> str:
    """Concatenate the ``output_text`` parts of an OpenAI Responses payload."""
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


def _call_subagent(
    *,
    system_prompt: str,
    user_payload: dict[str, Any],
    settings: AppSettings,
) -> tuple[Any, str | None]:
    """One bounded model call to the OpenAI Responses API, shared by the
    retriever and the rememberer. Returns the parsed JSON the model emitted and
    the provider response id. Every failure -- missing credentials, network,
    HTTP error, non-JSON -- raises ``AIJudgmentFailure``; the caller turns that
    into an ``ai_judgments`` row.

    ``store: False`` and ``model = settings.model_name`` match every other
    bounded AI call. Both subagents run host-side, so this works identically in
    the API process and the worker process.
    """
    if settings.openai_api_key is None:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_CREDENTIALS",
            safe_reason="memory subagent requires ARIEL_OPENAI_API_KEY",
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
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, sort_keys=True),
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
            safe_reason="memory subagent model timed out",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    except httpx.HTTPError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory subagent model network request failed",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    if response.status_code >= 400:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason=f"memory subagent model returned HTTP {response.status_code}",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_REQUIRED",
            safe_reason="memory subagent provider returned invalid JSON",
            retryable=True,
            parse_status="missing_output",
            validation_status="not_validated",
        ) from exc
    raw_id = response_payload.get("id") if isinstance(response_payload, dict) else None
    provider_response_id = raw_id if isinstance(raw_id, str) else None
    output = response_payload.get("output") if isinstance(response_payload, dict) else None
    try:
        return json.loads(_extract_output_text(output)), provider_response_id
    except json.JSONDecodeError as exc:
        raise AIJudgmentFailure(
            code="E_AI_JUDGMENT_INVALID_JSON",
            safe_reason="memory subagent model returned malformed JSON",
            retryable=False,
            parse_status="invalid_json",
            validation_status="invalid",
            provider_response_id=provider_response_id,
        ) from exc


def _schema_failure(reason: str) -> AIJudgmentFailure:
    """An ``AIJudgmentFailure`` for a malformed subagent JSON shape."""
    return AIJudgmentFailure(
        code="E_AI_JUDGMENT_SCHEMA",
        safe_reason=reason,
        retryable=False,
        parse_status="schema_invalid",
        validation_status="invalid",
    )


def _validation_failure(reason: str) -> AIJudgmentFailure:
    """An ``AIJudgmentFailure`` for subagent JSON that parsed but is invalid --
    e.g. a fact id the model invented."""
    return AIJudgmentFailure(
        code="E_AI_JUDGMENT_VALIDATION",
        safe_reason=reason,
        retryable=False,
        parse_status="parsed",
        validation_status="invalid",
    )


def _write_judgment(
    db: Session,
    *,
    judgment_type: Literal["memory_recall", "memory_remember"],
    prompt_version: str,
    source_type: str,
    source_id: str,
    status: Literal["succeeded", "failed"],
    settings: AppSettings,
    input_summary: str,
    input_refs: dict[str, Any],
    output: dict[str, Any],
    selected: list[dict[str, Any]],
    provider_response_id: str | None,
    now: datetime,
    new_id_fn: Callable[[str], str],
    failure: AIJudgmentFailure | None = None,
) -> None:
    """Write the one ``ai_judgments`` row that audits a subagent call. Called on
    both the success and the failure path -- ``failure`` carries the parse and
    validation status when the call failed."""
    db.add(
        AIJudgmentRecord(
            id=new_id_fn("ajg"),
            judgment_type=judgment_type,
            source_type=source_type,
            source_id=source_id,
            status=status,
            model=settings.model_name,
            prompt_version=prompt_version,
            provider_response_id=provider_response_id,
            input_summary=input_summary,
            input_refs=input_refs,
            selected=selected,
            omitted=[],
            output=output,
            rationale=None,
            uncertainty=None,
            confidence=None,
            parse_status=failure.parse_status if failure is not None else "parsed",
            validation_status=failure.validation_status if failure is not None else "valid",
            failure_code=failure.code if failure is not None else None,
            failure_reason=failure.safe_reason if failure is not None else None,
            created_at=now,
            updated_at=now,
        )
    )


# ---------------------------------------------------------------------------
# The retriever subagent
# ---------------------------------------------------------------------------
def _validated_retrieval(raw_payload: Any, *, candidate_ids: set[str]) -> list[str]:
    """Narrow the retriever's raw JSON into the list of selected fact ids, or
    fail closed. The contract is ``{"facts": ["<fact_id>", ...]}``; an id the
    model invented, a duplicate, or any malformed field raises -- no partial
    parse."""
    if not isinstance(raw_payload, dict):
        raise _schema_failure("memory retriever returned a non-object JSON value")
    facts_raw = raw_payload.get("facts")
    if not isinstance(facts_raw, list):
        raise _schema_failure("memory retriever JSON missing a facts array")
    selected: list[str] = []
    for item in facts_raw:
        if not isinstance(item, str) or not item:
            raise _schema_failure("memory retriever facts entries must be fact ids")
        if item not in candidate_ids:
            raise _validation_failure("memory retriever selected an unknown fact id")
        if item in selected:
            raise _validation_failure("memory retriever selected a duplicate fact id")
        selected.append(item)
    return selected


def run_retriever(
    *,
    session_factory: sessionmaker[Session],
    query: str,
    source_type: str,
    source_id: str,
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
) -> list[MemoryFactRecord]:
    """Run the retriever subagent: decide which stored facts matter for a wake.

    ``query`` is the wake context (a user message or a proactive case summary).
    Candidates are gathered deterministically, the model selects the relevant
    subset, the selection is validated, and the selected facts'
    ``last_recalled_at`` is stamped. Returns the selected facts, ordered as
    gathered.

    Every call writes one ``ai_judgments`` row (``judgment_type=memory_recall``)
    on both success and failure. Retriever failure is non-fatal by contract:
    this function raises ``AIJudgmentFailure`` on a failed model call after
    auditing it, and the caller (the turn engine) proceeds on the profile and
    digest alone.
    """
    with session_factory() as db:
        with db.begin():
            candidates = gather_candidates(
                db,
                query=query,
                settings=settings,
                limit=settings.memory_recall_candidate_limit,
            )
            candidate_payload = [{"id": fact.id, "content": fact.content} for fact in candidates]
            candidate_ids = {fact.id for fact in candidates}

    # The bounded model call, then validation -- a failure in either path is
    # audited as one failed memory_recall row and re-raised.
    try:
        raw, provider_response_id = _call_subagent(
            system_prompt=_RETRIEVER_PROMPT,
            user_payload={
                "prompt_version": RETRIEVER_PROMPT_VERSION,
                "wake_context": redact_text(query),
                "candidate_facts": candidate_payload,
            },
            settings=settings,
        )
        try:
            selected_ids = _validated_retrieval(raw, candidate_ids=candidate_ids)
        except AIJudgmentFailure as exc:
            exc.provider_response_id = provider_response_id
            raise
    except AIJudgmentFailure as failure:
        with session_factory() as db:
            with db.begin():
                _write_judgment(
                    db,
                    judgment_type="memory_recall",
                    prompt_version=RETRIEVER_PROMPT_VERSION,
                    source_type=source_type,
                    source_id=source_id,
                    status="failed",
                    settings=settings,
                    input_summary="memory retrieval for a deliberative wake",
                    input_refs={"candidate_count": len(candidate_payload)},
                    output={},
                    selected=[],
                    provider_response_id=failure.provider_response_id,
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                    failure=failure,
                )
        raise

    with session_factory() as db:
        with db.begin():
            now = now_fn()
            selected = list(
                db.scalars(
                    select(MemoryFactRecord).where(MemoryFactRecord.id.in_(selected_ids))
                ).all()
            )
            for fact in selected:
                fact.last_recalled_at = now
            _write_judgment(
                db,
                judgment_type="memory_recall",
                prompt_version=RETRIEVER_PROMPT_VERSION,
                source_type=source_type,
                source_id=source_id,
                status="succeeded",
                settings=settings,
                input_summary="memory retrieval for a deliberative wake",
                input_refs={"candidate_count": len(candidate_payload)},
                output={"recalled_count": len(selected)},
                selected=[{"fact_id": fact_id} for fact_id in selected_ids],
                provider_response_id=provider_response_id,
                now=now,
                new_id_fn=new_id_fn,
            )
    # Return the selected facts in gather order: stable and similarity-ranked.
    by_id = {fact.id: fact for fact in selected}
    return [by_id[fact_id] for fact_id in selected_ids if fact_id in by_id]


# ---------------------------------------------------------------------------
# The rememberer subagent
# ---------------------------------------------------------------------------
def _validated_rememberer_output(raw_payload: Any, *, candidate_ids: set[str]) -> RemembererOutput:
    """Narrow the rememberer's raw JSON into a :class:`RemembererOutput`, or fail
    closed.

    The contract is ``{"operations": [...], "profile": <str|null>,
    "digest": <str|null>}``. Each operation is ``{"op": "write", "content": ...}``,
    ``{"op": "edit", "fact_id": ..., "content": ...}``, or
    ``{"op": "forget", "fact_id": ...}``. An edit or forget of an id the model
    invented, or any malformed field, raises -- there is no partial parse.
    """
    if not isinstance(raw_payload, dict):
        raise _schema_failure("memory rememberer returned a non-object JSON value")
    operations_raw = raw_payload.get("operations")
    profile_raw = raw_payload.get("profile")
    digest_raw = raw_payload.get("digest")
    if not isinstance(operations_raw, list):
        raise _schema_failure("memory rememberer JSON missing an operations array")
    if profile_raw is not None and not isinstance(profile_raw, str):
        raise _schema_failure("memory rememberer profile must be a string or null")
    if digest_raw is not None and not isinstance(digest_raw, str):
        raise _schema_failure("memory rememberer digest must be a string or null")

    operations: list[FactOperation] = []
    for item in operations_raw:
        if not isinstance(item, dict):
            raise _schema_failure("memory rememberer operations entries must be objects")
        op = item.get("op")
        if op == "write":
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise _schema_failure("memory rememberer write operation missing content")
            operations.append(FactOperation(op="write", content=content.strip(), fact_id=""))
        elif op == "edit":
            fact_id = item.get("fact_id")
            content = item.get("content")
            if not isinstance(fact_id, str) or not fact_id:
                raise _schema_failure("memory rememberer edit operation missing fact_id")
            if not isinstance(content, str) or not content.strip():
                raise _schema_failure("memory rememberer edit operation missing content")
            if fact_id not in candidate_ids:
                raise _validation_failure("memory rememberer edited an unknown fact id")
            operations.append(FactOperation(op="edit", content=content.strip(), fact_id=fact_id))
        elif op == "forget":
            fact_id = item.get("fact_id")
            if not isinstance(fact_id, str) or not fact_id:
                raise _schema_failure("memory rememberer forget operation missing fact_id")
            if fact_id not in candidate_ids:
                raise _validation_failure("memory rememberer forgot an unknown fact id")
            operations.append(FactOperation(op="forget", content="", fact_id=fact_id))
        else:
            raise _schema_failure("memory rememberer operation has an unknown op")

    return RemembererOutput(
        operations=tuple(operations),
        profile=profile_raw,
        digest=digest_raw,
    )


def apply_rememberer_output(
    db: Session,
    output: RemembererOutput,
    *,
    source_turn_id: str | None,
    settings: AppSettings,
    now: datetime,
    new_id_fn: Callable[[str], str],
) -> list[str]:
    """Apply a validated rememberer judgment to the fact store inside the
    caller's transaction. A ``write`` inserts an ``active`` fact; an ``edit``
    rewrites a fact's content; a ``forget`` sets ``status=forgotten``. The
    embedding of each written or edited fact is computed via :func:`embed_text`.

    The profile and digest are not applied here: ``run_rememberer`` writes the
    profile, and the digest is returned to the caller (the turn engine writes it
    onto the session). Returns the ids of facts written or edited, for the audit
    row. Applying operations is a rail; deciding them is the subagent's
    judgment.
    """
    touched: list[str] = []
    for operation in output.operations:
        match operation.op:
            case "write":
                written = MemoryFactRecord(
                    id=new_id_fn("mfa"),
                    content=operation.content,
                    status="active",
                    source_turn_id=source_turn_id,
                    source_excerpt=None,
                    embedding=embed_text(operation.content, settings=settings),
                    created_at=now,
                    updated_at=now,
                    last_recalled_at=None,
                )
                db.add(written)
                touched.append(written.id)
            case "edit":
                edited = db.get(MemoryFactRecord, operation.fact_id)
                if edited is None:
                    raise RuntimeError(
                        f"rememberer edit references missing fact {operation.fact_id}"
                    )
                edited.content = operation.content
                edited.embedding = embed_text(operation.content, settings=settings)
                edited.updated_at = now
                touched.append(edited.id)
            case "forget":
                forgotten = db.get(MemoryFactRecord, operation.fact_id)
                if forgotten is None:
                    raise RuntimeError(
                        f"rememberer forget references missing fact {operation.fact_id}"
                    )
                forgotten.status = "forgotten"
                forgotten.updated_at = now
    db.flush()
    return touched


def _conversation_for_turn(db: Session, turn_id: str) -> tuple[dict[str, str], str | None]:
    """Read a completed turn into the rememberer's review material and return
    its session id. Raises when the turn is missing -- a malformed task."""
    turn = db.get(TurnRecord, turn_id)
    if turn is None:
        raise RuntimeError(f"memory_remember task references missing turn {turn_id}")
    conversation = {
        "user_message": redact_text(turn.user_message),
        "assistant_message": redact_text(turn.assistant_message or ""),
    }
    return conversation, turn.session_id


def run_rememberer(
    *,
    session_factory: sessionmaker[Session],
    settings: AppSettings,
    now_fn: Callable[[], datetime],
    new_id_fn: Callable[[str], str],
    trigger: Literal["note", "turn", "sweep"] = "note",
    note: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> RemembererOutput:
    """Run the rememberer subagent: maintain the fact store, the profile, and the
    session digest from one bounded model call.

    The three triggers differ only in what material is reviewed and which
    documents can change; one model call and one ``apply`` path serve all three:

    - ``"turn"`` -- a completed turn (``turn_id``) or a closing session. The
      conversation is the material; facts and the session digest are updated,
      and the profile when something durable changed. Enqueued after every turn
      and on session rotation.
    - ``"note"`` -- an on-demand ``memory.remember`` note. ``note`` is the
      material; ``session_id`` scopes the digest.
    - ``"sweep"`` -- the periodic store sweep. The active fact store is the
      material; the rememberer prunes stale facts and re-tightens the profile,
      and this function hard-deletes rows left ``forgotten`` past the retention
      window. No digest is touched.

    Every call writes one ``ai_judgments`` row (``judgment_type=memory_remember``)
    on both success and failure. On a failed model call this raises
    ``AIJudgmentFailure`` after auditing it; the background task then retries
    under the normal retry policy.
    """
    # 1. Gather the review material and the existing-fact candidates, and
    #    hard-delete long-forgotten rows when sweeping.
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            conversation: dict[str, str] | None = None
            effective_session_id = session_id
            review_text = note or ""
            if trigger == "turn":
                if turn_id is None:
                    raise RuntimeError("memory_remember turn trigger requires a turn_id")
                conversation, effective_session_id = _conversation_for_turn(db, turn_id)
                review_text = " ".join(conversation.values())
            elif trigger == "note":
                if not (note and note.strip()):
                    raise RuntimeError("memory_remember note trigger requires a note")

            if trigger == "sweep":
                long_forgotten = list(
                    db.scalars(
                        select(MemoryFactRecord.id).where(
                            MemoryFactRecord.status == "forgotten",
                            MemoryFactRecord.updated_at < now - _FORGOTTEN_RETENTION,
                        )
                    ).all()
                )
                if long_forgotten:
                    db.execute(
                        delete(MemoryFactRecord).where(MemoryFactRecord.id.in_(long_forgotten))
                    )
                deleted = len(long_forgotten)
                candidates = list_active_facts(db, limit=settings.memory_recall_candidate_limit)
            else:
                deleted = 0
                candidates = gather_candidates(
                    db,
                    query=review_text,
                    settings=settings,
                    limit=settings.memory_recall_candidate_limit,
                )

            profile = read_profile(db)
            session_digest: str | None = None
            if effective_session_id is not None:
                session = db.get(SessionRecord, effective_session_id)
                session_digest = session.digest if session is not None else None
            candidate_payload = [{"id": fact.id, "content": fact.content} for fact in candidates]
            candidate_ids = {fact.id for fact in candidates}

    # The audit row's source identifies what was reviewed: a session for a turn
    # or a session-scoped note, the store for a sweep, or unscoped memory for an
    # on-demand note made outside any session.
    if trigger == "sweep":
        audit_source_type, audit_source_id = "memory_sweep", "memory_sweep"
    elif effective_session_id is not None:
        audit_source_type, audit_source_id = "session", effective_session_id
    else:
        audit_source_type, audit_source_id = "memory", "memory"
    input_refs: dict[str, Any] = {
        "trigger": trigger,
        "candidate_count": len(candidate_payload),
    }
    if turn_id is not None:
        input_refs["turn_id"] = turn_id

    # 2. The bounded model call, then validation -- a failure in either path is
    #    audited as one failed memory_remember row and re-raised.
    try:
        raw, provider_response_id = _call_subagent(
            system_prompt=_REMEMBERER_PROMPT,
            user_payload={
                "prompt_version": REMEMBERER_PROMPT_VERSION,
                "trigger": trigger,
                "profile": profile,
                "session_digest": session_digest,
                "conversation": conversation,
                "note": redact_text(note) if note else None,
                "existing_facts": candidate_payload,
            },
            settings=settings,
        )
        try:
            output = _validated_rememberer_output(raw, candidate_ids=candidate_ids)
        except AIJudgmentFailure as exc:
            exc.provider_response_id = provider_response_id
            raise
    except AIJudgmentFailure as failure:
        with session_factory() as db:
            with db.begin():
                _write_judgment(
                    db,
                    judgment_type="memory_remember",
                    prompt_version=REMEMBERER_PROMPT_VERSION,
                    source_type=audit_source_type,
                    source_id=audit_source_id,
                    status="failed",
                    settings=settings,
                    input_summary=f"memory rememberer ({trigger})",
                    input_refs=input_refs,
                    output={},
                    selected=[],
                    provider_response_id=failure.provider_response_id,
                    now=now_fn(),
                    new_id_fn=new_id_fn,
                    failure=failure,
                )
        raise

    # 3. Apply the judgment: fact operations, the profile, and the digest, then
    #    audit -- all in one transaction.
    with session_factory() as db:
        with db.begin():
            now = now_fn()
            touched = apply_rememberer_output(
                db,
                output,
                source_turn_id=turn_id,
                settings=settings,
                now=now,
                new_id_fn=new_id_fn,
            )
            if output.profile is not None:
                write_profile(db, content=output.profile, now=now)
            if output.digest is not None and effective_session_id is not None:
                session = db.get(SessionRecord, effective_session_id)
                if session is not None:
                    session.digest = output.digest
            _write_judgment(
                db,
                judgment_type="memory_remember",
                prompt_version=REMEMBERER_PROMPT_VERSION,
                source_type=audit_source_type,
                source_id=audit_source_id,
                status="succeeded",
                settings=settings,
                input_summary=f"memory rememberer ({trigger})",
                input_refs=input_refs,
                output={
                    "operation_count": len(output.operations),
                    "profile_rewritten": output.profile is not None,
                    "digest_rewritten": output.digest is not None,
                    "hard_deleted_forgotten": deleted,
                },
                selected=[{"fact_id": fact_id} for fact_id in touched],
                provider_response_id=provider_response_id,
                now=now,
                new_id_fn=new_id_fn,
            )
    return output


# ---------------------------------------------------------------------------
# Background-task enqueuers
#
# These build the rememberer's background-task rows. The worker dispatch that
# runs them is wired in Phase 5; enqueueing is the memory module's concern.
# ---------------------------------------------------------------------------
def enqueue_memory_remember(
    db: Session,
    *,
    turn_id: str,
    now: datetime,
) -> str:
    """Enqueue a ``memory_remember`` background task for a completed turn (or a
    closing session's final turn). Idempotent per turn: a second enqueue for the
    same turn returns the existing task. Returns the task id."""
    task = enqueue_background_task(
        db,
        task_type="memory_remember",
        idempotency_key=f"memory_remember:{turn_id}",
        payload={"turn_id": turn_id},
        now=now,
    )
    return task.id


def enqueue_due_memory_sweep(
    db: Session,
    *,
    settings: AppSettings,
    now: datetime,
) -> str | None:
    """Self-gating periodic sweep enqueuer: enqueue one ``memory_sweep`` task
    when the last sweep is older than the configured interval, otherwise enqueue
    nothing. Returns the new task id, or ``None`` when a sweep is not yet due.
    Called from the worker's enqueue-due pass."""
    interval = timedelta(seconds=settings.memory_sweep_interval_seconds)
    recent_sweep = db.scalar(
        select(BackgroundTaskRecord.id)
        .where(
            BackgroundTaskRecord.task_type == "memory_sweep",
            BackgroundTaskRecord.created_at > now - interval,
        )
        .limit(1)
    )
    if recent_sweep is not None:
        return None
    task = enqueue_background_task(db, task_type="memory_sweep", payload={}, now=now)
    return task.id
