# s3 pr-03 implementation notes

## delivered scope

- hardened retrieval synthesis routing in `action_runtime`:
  - any turn that executes retrieval (`cap.search.web`, `cap.search.news`, `cap.weather.forecast`)
    now emits grounded narrative in `assistant.message`.
  - grounded synthesis no longer requires retrieval-only proposal sets.
  - mixed turns with retrieval keep `assistant.sources[]` populated and citation markers in text.
- preserved non-retrieval inspectability for mixed turns:
  - non-retrieval proposal outcomes remain in `turn.surface_action_lifecycle[]` and `turn.events[]`.
  - raw `action result (...)` appendix text is no longer surfaced for retrieval-backed turns.
- added fail-closed conflict handling for same-claim evidence disagreement:
  - deterministic claim-signature heuristic over cited snippets (`<attribute> of <entity> is <value>`
    and possessive variant).
  - conflicting signatures force uncertainty language plus concrete recovery guidance.
- retained partial/failure disclosure:
  - mixed retrieval failures continue to surface explicit partial/retry guidance while preserving
    citation-gating invariants.

## hardening decisions

- removed broad `\"<entity> is <value>\"` conflict matching fallback to avoid false conflicts when
  snippets describe different attributes of the same entity.
- limited conflict extraction to attribute-scoped claim forms to bias toward precision over recall
  (explicitly aligned to pr-03 non-goals and deterministic-mvp constraints).
- maintained existing artifact persistence/citation synchronization path, avoiding contract drift in
  artifact and surfaced response schemas.

## test coverage added/updated

- new integration suite: `tests/integration/test_s3_pr03_acceptance.py`
  - conflicting evidence => uncertainty + recovery
  - mixed retrieval + non-retrieval for web/news/weather keeps grounded citations + sources
  - unsupported model factual assertion suppression in retrieval-backed mixed turns
  - mixed partial retrieval failure disclosure and recoverability
  - mixed non-retrieval denial remains inspectable via lifecycle without appendix regression
  - false-positive guard: distinct facts about the same entity do not trigger conflict mode
- updated legacy slice-3 test expectation:
  - `tests/integration/test_s3_pr01_acceptance.py`
  - mixed retrieval/non-retrieval flow now asserts grounded message + sources + inspectable lifecycle.

## verification run for this implementation

- targeted:
  - `.venv/bin/python -m pytest tests/integration/test_s3_pr03_acceptance.py`
  - `.venv/bin/python -m pytest tests/integration/test_s3_pr01_acceptance.py tests/integration/test_s3_pr02_acceptance.py tests/integration/test_s3_pr03_acceptance.py`
- full:
  - `make verify`
  - `make e2e`
- manual cli verification:
  - exercised mixed retrieval + non-retrieval turn to confirm:
    - grounded `assistant.message`
    - synchronized `assistant.sources[]`
    - non-retrieval lifecycle entries remain present
  - exercised conflicting-evidence turn to confirm uncertainty-first, recovery-oriented response text.
