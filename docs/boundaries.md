# Boundaries

## Scope

This document covers how values should change shape when they cross a boundary — between your code and external systems, between modules, or between trust levels.

## Goal

Boundary conversion produces a type narrow enough that downstream code never needs to re-examine or re-validate the value. The type itself carries the guarantee. A branded `FooKey` can't be an unvalidated string. A classified absence can't be an ambiguous `null`. Internal code works with these narrow types and never looks back at the raw form.

## Untrusted Input

Untrusted input comes from outside the system: RPC requests, webhooks, third-party APIs.

- Parse, validate, and narrow to a specific typed form where untrusted data enters. Failures are typed errors — bad external input is expected.
- The result of parsing is the narrow internal type: a brand, a domain object, an internal ID. That type flows through all downstream code.
- Do not re-parse or re-validate deeper in the stack. If the type is right, the value is right.
- Parse only the single expected shape. Do not add branches for alternative shapes "just in case" — those are dead code that hides bugs.
- See [errors.md](errors.md) for classifying `None` and absence at the boundary.

## Trusted Data

Trusted data comes from systems we control: our database, our own modules, our persisted state.

- If trusted data does not match the expected shape, that is a defect — something we wrote or stored is wrong.
- Do not silently re-normalize trusted data. If a database value is not in the right form, the write path or schema is the problem — fix it there.
- Do not add redundant validation across layers. Tighten the source or the persisted representation instead.

## Internal Representation

- Keep values in their narrowest, richest typed form. Do not widen back to primitives or strings until the moment an external consumer requires it.
- Do not pre-convert to lossy forms into intermediate variables. If a value has meaning beyond its primitive shape, it should have a type that reflects that meaning — not flow through code as a bare `string` or `number`.

## Outgoing Conversion

- Convert to a lossy or primitive form only at the moment it is needed — inline at the consumption site.
