# Function Parameters

## Scope

This document covers parameter shape rules.

## Rules

- Config, builder, and boundary APIs should take a single object parameter.
- Prefer collapsing multiple business fields into one named payload object rather than adding more positional parameters.
- Boundary-owned replay identity should live in a named field such as `idempotency_key` inside the boundary payload or options object.
- Do not thread internal replay bookkeeping through domain helper APIs; keep replay identity at the workflow boundary layer.
- Optional parameters should always live in an object parameter.
- Prefer shallow object shapes by default.
- Keep fields nested when they belong to a real named sub-concern, phase, or subsystem.
- Small pure helpers and primitives may remain positional when the arguments are obvious and tightly coupled.
