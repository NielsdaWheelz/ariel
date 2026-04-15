# Correctness

## Scope

This document covers system abnormality classification and repository-wide correctness invariants.

## Abnormalities

- Expected system-level abnormalities must be modeled and handled in code.
- Unexpected abnormalities indicate a broken invariant and should trigger investigation.
- Expected abnormalities include server restarts and transient service failures within the applicable retry budget.
- Unexpected abnormalities include service failures that persist beyond the applicable retry budget.
- Retry budgets define the boundary between expected transient failure and unexpected persistent failure.

## Invariants

- If concurrent execution or crash-and-replay can produce an incorrect result, it is a bug.
- Every operation must correspond to some valid sequential ordering of all concurrent operations, including across crash-and-replay.
- Every committed external side effect must be discoverable during recovery; volatile reads alone are not sufficient.
- Read-only operations that span multiple systems must handle transient inconsistency from concurrent modification.
- Prefer static enforcement of correctness invariants where possible (type annotations, mypy strict, Pydantic validation).
- See [mutation-ordering.md](mutation-ordering.md) for cross-system ordering.

## Untrusted Data

- See [boundaries.md](boundaries.md) for parsing, validation, and trusted-vs-untrusted rules.
