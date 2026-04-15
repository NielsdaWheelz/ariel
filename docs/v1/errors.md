# Errors

## Scope

This document covers error and defect modeling, `None` normalization, and runtime invariant checks.

## Errors and Defects

- Construct an error type only in the code that detects the condition it represents.
- When branching on an error, consume the original error type completely and replace it with distinct branch-specific types.
- Use errors (custom exception classes) for expected, modelable failures.
- Use defects for broken invariants, impossible states, internal corruption, schema or code mismatch, and similar "should never happen" conditions.
- Handle errors as deeply as possible and propagate them upward only when needed.
- Defects are not normal application control flow.
- Do not convert defects into UI states, retryable business branches, persisted domain status fields, or other product-facing recovery paths.
- Observing a defect in production should trigger a code or operational change.
- Any intentional defect classification must include `justify-defect`.
- Any branch that discards an error must first narrow it to a specific exception class and include `justify-ignore-error`.

## `None`

- Do not use `X | None` in service or domain APIs to represent absence that still requires classification.
- Classify such absence immediately as a typed error or a defect.
- Raw `None` is only for foreign interfaces we do not control.
- Normalize raw nullable input at the boundary. See [boundaries.md](boundaries.md) for the general ingress rule.
- Use `X | None` when optionality is itself the successful result.
- Use a typed error (specific exception class) when absence is an expected application-level failure.
- Use a defect when absence violates an invariant.
- Our own helpers, services, and internal state should not accept or return raw `None` unless interop makes a better representation materially worse.

## Service Invariants

- Represent parameter validity in types, `NewType`, and Pydantic-validated canonical values.
- If malformedness is knowable locally, validate once into an owned type, `NewType`, or Pydantic model and carry that type through the system.
- Do not hide local representability checks inside render, quote, or encode helpers.
- Renderers, quoters, and encoders should assume already-owned local types and only perform boundary-specific escaping or formatting.
- Do not use runtime service-boundary guards for parameter validity that should be encoded in types or `NewType`.
- Runtime checks in service code should enforce remaining invariants that cannot be expressed cleanly in the type system.
- Such checks must include `justify-service-invariant-check` explaining why the invariant is not represented in types, `NewType`, or Pydantic-validated canonical values.
- Violations of such invariants are defects.
