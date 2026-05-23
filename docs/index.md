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
- [coordination.md](coordination.md): PostgreSQL-native coordination — advisory locks, idempotency, and the background-task queue
- [mutation-ordering.md](mutation-ordering.md): ordering mutations across systems and module boundaries

### Data and types

- [boundaries.md](boundaries.md): data representation at ingress, internal, and egress edges
- [errors.md](errors.md): error and defect modeling, null classification
- [keys-and-identities.md](keys-and-identities.md): identity naming, brands, and sealing
- [json-values.md](json-values.md): structured JSON values
- [generated-text.md](generated-text.md): escaping and quoting at generated-text boundaries
- [jarvis-system-prompt.md](jarvis-system-prompt.md): Jarvis-style assistant
  prompt, persona, tool policy, proactivity policy, and eval checklist

### Code style

- [ai-first.md](ai-first.md): AI owns judgment, deterministic code owns rails
- [personal-agent-sota-roadmap.md](personal-agent-sota-roadmap.md):
  OpenClaw-class parity, Claw-variant lessons, and personal-agent SOTA roadmap
- [north-star-cutover.md](north-star-cutover.md): single-`run`,
  Agency-centered product architecture
- [simplicity.md](simplicity.md): fewer code paths, no speculative surface
- [function-parameters.md](function-parameters.md): parameter conventions
- [control-flow.md](control-flow.md): exhaustive branching and race-safety
- [conventions.md](conventions.md): small conventions (constants, generics, base64)

### Platform

- [codebase.md](codebase.md): tech stack, repo structure, imports, and module boundaries
- [database.md](database.md): PostgreSQL schema, queries, and transactions

### Product operations

- [production-runbook.md](production-runbook.md): production deployment, operations,
  ambient Discord chat, deterministic slash operations, health checks, recovery,
  and acceptance criteria
- [dev-environment.md](dev-environment.md): isolated local dev stack — `.env.dev`,
  parallel postgres on `:5435`, dev API on `:8001`, coexistence with the prod
  systemd services
- [google-reconnect.md](google-reconnect.md): how to (re)connect Google and grant
  identity + write scopes (Gmail send/modify, Calendar write, Drive share)

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
