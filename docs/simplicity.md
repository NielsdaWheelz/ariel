# Simplicity

## Scope

This document covers repository-wide implementation simplicity rules.

## Rules

- When there are multiple reasonable ways to write something, prefer fewer lines and fewer characters within reason.
- Default to fewer code paths.
- Each additional code path should be justifiable.
- Do not add speculative API surface.
- Do not add optional parameters, options, or flags until a real call site needs them.
- Apply the same bias to error handling, schema validation, and branching.
- Do not add code paths for scenarios that cannot be constructed.
- Expose each capability in one primary form. Do not expose interchangeable duplicate APIs for the same capability.
- If a capability already exists in a module, prefer using it over introducing a near-duplicate.
- Simplicity does not justify deterministic judgment. Follow [ai-first.md](ai-first.md):
  prefer one clear AI/subagent decision path over many handcrafted branches,
  thresholds, classifiers, and fallback prose paths.
- Deterministic helpers must be narrow services with a clear current need and a
  clear removal path.
