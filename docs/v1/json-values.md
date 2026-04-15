# JSON Values

## Scope

This document covers structured JSON values.

## Rules

- Keep semantically structured JSON as Pydantic models or `dict[str, Any]` in code and API DTOs rather than stringifying it.
- Persist structural JSON in PostgreSQL `jsonb`, not `text`.
- At the PostgreSQL boundary, represent `json` and `jsonb` columns as SQLAlchemy `JSONB` so SQL `NULL` stays distinct from JSON `null`.
- Wrap outgoing bind values through SQLAlchemy's JSON type handling.
- Decode or Pydantic-validate `JSONB` values at the query boundary when the distinction is no longer needed. See [boundaries.md](boundaries.md) for the general boundary conversion rules.
- Do not use `is` or `==` for potentially structural JSON values without care.
- Narrow to primitives first when that is the intent.
- Otherwise use deep comparison (`json.dumps(x, sort_keys=True)` or a recursive equality helper) for structural equality.
- For structural equality and dedup in Python collections, use helpers such as a canonical `json.dumps` with `sort_keys=True` for hashing or a set-based dedup over serialized forms.
