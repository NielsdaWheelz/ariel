# Conventions

## Scope

This document covers small implementation conventions that do not belong to a larger topic.

## Named Constants

- Extract a value into a named constant when the name conveys information beyond what the usage site already says.
- Keep a value inline when it is inherently part of the expression.

## Generic Type Parameters

- Constrain on composite types rather than decomposing their inner type parameters.
- Prefer `T: SomeType` (bound `TypeVar`) or `Protocol` over separate type parameters for `SomeType`'s inner parts.
- Use `TypeVar` with `bound` or `Protocol` to express constraints; use attribute access or `get_type_hints` to extract constituent types.
- Introduce separate type parameters only when callers need to specify or constrain them independently.

## Base64

- Default to base64url encoding rather than base64.
- Use base64 only with `justify-base64-over-base64url`.
