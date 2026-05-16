"""Long-memory eval cases for the WS-10 regression suite, as plain data.

Each case is a plain dict. ``test_memory_eval_suite.py`` seeds the canonical
memory these cases describe, maps the per-case memory labels to the real
assertion ids, and hands the resulting case list to ``run_memory_eval``. The
labels (``expect_labels`` / ``forbid_labels`` / ``forbid_label`` / signal
rankings) are placeholders the test resolves against its seeded-id map; every
other field is the final value ``run_memory_eval`` consumes.

``LONG_MEMORY_EVAL_CASES`` runs against the real hybrid retrieval pipeline as a
single ``run_memory_eval`` call over the whole case list. ``ADVERSARIAL_EVAL_CASES``
runs with the vector and lexical signal functions monkeypatched to fixed rankings;
because each case carries its own per-signal rankings, the suite runs each case as
its own single-case ``run_memory_eval`` invocation, replaying every case with one
signal disabled to prove the suite fails under vector-only or keyword-only retrieval.
"""

from __future__ import annotations

from typing import Any

# Each natural case maps to one memory.md long-memory eval requirement. They run
# against the genuine Reciprocal Rank Fusion pipeline; the canonical memory each
# describes is seeded by the suite test.
LONG_MEMORY_EVAL_CASES: list[dict[str, Any]] = [
    {
        # Temporal validity decides the answer: of two milestone deadlines, only
        # the one whose validity interval still contains "now" carries the
        # temporal signal, so it outranks the expired one under fusion.
        "name": "temporal-validity-decisive",
        "query": "what is the current milestone deadline we should track",
        "expect_labels": ["temporal_valid"],
        "forbid_labels": ["temporal_stale"],
        "expected_kinds": ["semantic_assertion"],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 1,
    },
    {
        # A contradiction between two single-valued facts must surface as an
        # open conflict (uncertainty), never as one silently-settled answer.
        "name": "conflict-must-surface-as-uncertainty",
        "query": "when does phoenix ship",
        "expect_labels": [],
        "forbid_labels": [],
        "expected_kinds": [],
        "forbidden_texts": [],
        "expect_conflict": True,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # The correct answer is to abstain: an in-scope memory exists but does
        # not answer the query, so recall must surface nothing rather than drag
        # in an irrelevant memory.
        "name": "correct-answer-is-abstain",
        "query": "what is the abstainzone shipping deadline",
        "expect_labels": [],
        "forbid_labels": ["abstain_decoy"],
        "expected_kinds": [],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # Correction / supersession: a corrected assertion supersedes its
        # predecessor, so recall surfaces the new value and never the old one.
        "name": "correction-supersedes-stale-value",
        "query": "when will the zebra release land for users",
        "expect_labels": ["zebra_corrected"],
        "forbid_labels": ["zebra_original"],
        "expected_kinds": ["semantic_assertion"],
        "forbidden_texts": ["first guess"],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # Deletion / privacy-deletion compliance: a deleted assertion is gone
        # from recall, content and all.
        "name": "deletion-removes-memory-from-recall",
        "query": "what is the vendor onboarding secret code",
        "expect_labels": [],
        "forbid_labels": ["vendor_deleted"],
        "expected_kinds": [],
        "forbidden_texts": ["code indigo"],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # no_memory mode: a project scope bound to no_memory blocks recall
        # entirely, and the policy decision says so.
        "name": "no-memory-mode-blocks-recall",
        "query": "what is the notebook nomemoryzone deadline status",
        "expect_labels": [],
        "forbid_labels": ["nomemory_blocked"],
        "expected_kinds": [],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": True,
        "max_recalled_assertions": 8,
    },
    {
        # Proactive feedback: a memory learned through proactive deliberation
        # (candidate -> review -> active) is recalled like any other fact.
        "name": "proactive-feedback-memory-is-recalled",
        "query": "when is the deploy review scheduled",
        "expect_labels": ["proactive_memory"],
        "forbid_labels": [],
        "expected_kinds": ["semantic_assertion"],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # Negative-memory adherence: a rejected approach is retrieved and
        # surfaced as the negative_memory kind so Ariel does not repeat it.
        "name": "negative-memory-adherence",
        "query": "should we retry the espresso cache warmup approach",
        "expect_labels": ["negative_rejected"],
        "forbid_labels": [],
        "expected_kinds": ["negative_memory"],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # Graph-relationship reasoning: the answer is two hops away in the
        # entity graph, reachable only through the multi-hop graph signal.
        "name": "graph-relationship-reasoning",
        "query": "status of the migration initiative",
        "expect_labels": ["graph_two_hop"],
        "forbid_labels": [],
        "expected_kinds": ["semantic_assertion"],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 8,
    },
    {
        # Hot-index budget pressure: the hot index for a scope, rebuilt under a
        # tight token budget, is still well-formed and recallable as the
        # hot_index kind. The query is scoped to the hotindexzone project.
        "name": "hot-index-budget-pressure",
        "query": "give me the hotindexzone open questions overview",
        "expect_labels": ["hot_index_block"],
        "forbid_labels": [],
        "expected_kinds": ["hot_index"],
        "forbidden_texts": [],
        "expect_conflict": False,
        "expect_policy_blocked": False,
        "max_recalled_assertions": 30,
    },
]


# The adversarial cases prove no single retrieval signal suffices. Each correct
# answer is reachable only when both the vector and the lexical signal run: the
# vector signal alone yields one wrong memory, the lexical signal alone yields
# another, and only the fused pool contains the correct memory. Each case carries
# its own per-signal rankings, so the suite runs every case as a separate
# single-case ``run_memory_eval`` invocation; it replays the full set with the
# vector signal disabled (keyword-only) and again with the lexical signal disabled
# (vector-only), asserting the eval fails both times.
#
# ``vector_labels`` / ``lexical_labels`` are the ordered rankings the suite
# installs on ``_vector_signal`` / ``_lexical_signal``; the test resolves the
# labels to seeded assertion ids.
ADVERSARIAL_EVAL_CASES: list[dict[str, Any]] = [
    {
        # Vector similarity alone selects the wrong memory: the vector signal
        # ranks the decoy first and never reaches the correct memory, which is
        # pulled into the pool by the lexical signal.
        "name": "vector-similarity-alone-wrong",
        "query": "which landing note is the correct one",
        "expect_label": "fused_correct",
        "forbid_label": "vector_decoy",
        "vector_labels": ["vector_decoy", "fused_correct"],
        "lexical_labels": ["fused_correct"],
        "max_recalled_assertions": 1,
    },
    {
        # Keyword match alone selects the wrong memory: the lexical signal ranks
        # the decoy first and never reaches the correct memory, which is pulled
        # into the pool by the vector signal.
        "name": "keyword-match-alone-wrong",
        "query": "which release note should we trust",
        "expect_label": "fused_correct",
        "forbid_label": "lexical_decoy",
        "vector_labels": ["fused_correct"],
        "lexical_labels": ["lexical_decoy", "fused_correct"],
        "max_recalled_assertions": 1,
    },
]
