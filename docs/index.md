# Docs

## Role

This directory is the canonical home for repository documentation.

## Goals

- MECE organization: documents are mutually exclusive and collectively exhaustive.
- Concision
- Clear boundaries

## Docs

### Correctness and concurrency

- [correctness.md](correctness.md): abnormality classification and system invariants
- [operation-types.md](operation-types.md): operation complexity, idempotency, and transaction boundaries
- [concurrency.md](concurrency.md): linearization and concurrent execution
- [mutation-ordering.md](mutation-ordering.md): ordering mutations across systems and module boundaries

### Data and types

- [boundaries.md](boundaries.md): data representation at ingress, internal, and egress edges
- [errors.md](errors.md): error and defect modeling, null classification
- [keys-and-identities.md](keys-and-identities.md): identity naming, brands, and sealing
- [json-values.md](json-values.md): structured JSON values
- [generated-text.md](generated-text.md): escaping and quoting at generated-text boundaries

### Code style

- [ai-first.md](ai-first.md): AI owns judgment, deterministic code owns rails
- [north-star-cutover.md](north-star-cutover.md): hard cutover spec for the
  single-`run`, Agency-centered product architecture
- [run-program-cutover.md](run-program-cutover.md): hard cutover from the
  flat-JSON `run` call list to a sandboxed Python `run` program with typed
  syscall host functions
- [simplicity.md](simplicity.md): fewer code paths, no speculative surface
- [function-parameters.md](function-parameters.md): parameter conventions
- [control-flow.md](control-flow.md): exhaustive branching and race-safety
- [conventions.md](conventions.md): small conventions (constants, generics, base64)

### Platform

- [codebase.md](codebase.md): tech stack, repo structure, imports, and module boundaries
- [database.md](database.md): PostgreSQL schema, queries, and transactions
- [schema-consolidation-cutover.md](schema-consolidation-cutover.md): phased plan to consolidate the schema from 86 tables toward ~72
- [schema-cross-cutting-cutover.md](schema-cross-cutting-cutover.md): Phase 4 of the consolidation — job queues, write ledgers, event logs

### Product operations

- [production-runbook.md](production-runbook.md): production deployment, operations,
  ambient Discord chat, deterministic slash operations, health checks, recovery,
  and acceptance criteria

### Modules

- [modules/index.md](modules/index.md): infrastructure-module and feature docs

## Placement Rules

- Each rule lives in exactly one document.
- Put content in the narrowest document that fully owns it.
- Link to related docs instead of restating them.
- If two docs need the same text, the split is wrong.
- If a document covers multiple unrelated topics, split it.
- Small docs are fine when they keep ownership and boundaries sharp.
- Keep repo-wide rule docs flat until a topic clearly needs its own directory.
- Use subdirectories for service-owned, module-owned, or feature-owned docs when that keeps them separate from repo-wide rules.
- Avoid over-categorized hierarchies and umbrella docs with weak boundaries.

## Rule Shape

- Prefer unconditional rules.
- Do not write soft rules with words like `usually`, `generally`, or `normally`.
- State the unconditional rule or the explicit exception.
- Prefer narrowing scope or splitting a rule over adding exceptions.
- If a rule needs many exceptions, the rule or the document boundary is probably wrong.

## Ownership

This file defines the documentation system itself: purpose and placement rules. It does not own product or codebase rules beyond that.
