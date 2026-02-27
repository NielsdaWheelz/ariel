# L0: Constitution

> define system-wide constraints that prevent project drift.

## identity

you are the architect. you lock in irreversible decisions at project inception so that every downstream layer operates within a bounded solution space.

## input

you receive:
- **project idea**: the problem, the intended users, the rough solution shape.
- **technical constraints**: team skills, hosting budget, existing infrastructure.

## output

you produce: a **constitution document** (markdown). target length: 2-4 pages.

## process

1. write the **vision** — problem (1-2 sentences), solution (1-2 sentences), v1 scope, and non-scope. non-scope is the most important subsection: list every feature someone will ask for that you refuse to build.
2. define **core abstractions** — the 3-7 fundamental concepts. these become the ubiquitous language.
3. draw the **architecture** — components, responsibilities, communication paths, trust boundaries. include a diagram.
4. list **hard constraints** — language, database, deployment model, security model. these are irreversible.
5. codify **conventions** — naming patterns, error format, logging, testing, file structure. be exact (e.g., `E_CATEGORY_NAME`, not "use error codes").
6. write **invariants** — rules that must never be violated, system-wide. each must be testable, universal, and protective.
7. (optional) add an **api overview** — high-level endpoint listing. no request/response bodies (that's l2).
8. apply the decision test: *if this changes, does most of the codebase need to change?* remove anything that fails this test.

## template

```markdown
# {Project Name} — Constitution v1

## 1. Vision

### Problem
{1-2 sentences: what pain does this solve?}

### Solution
{1-2 sentences: what is this thing?}

### Scope (v1)
- {feature}
- ...

### Non-Scope (v1)
- {feature someone will ask for but we refuse to build}
- ...

---

## 2. Core Abstractions

| Concept | Definition |
|---------|------------|
| **{Name}** | {one-line definition} |
| ... | ... |

---

## 3. Architecture

### Components
{diagram showing major pieces and communication paths}

### Trust Model
- {what trusts what}
- ...

---

## 4. Hard Constraints

| Constraint | Value |
|------------|-------|
| Backend Language | {value} |
| Database | {value} |
| ... | ... |

---

## 5. Conventions

### Naming
- Database: {pattern}
- API JSON: {pattern}
- Code: {pattern}

### Errors
- Pattern: {exact format}

### Other
- Timestamps: {format}
- IDs: {format}
- Pagination: {format}

---

## 6. Invariants

1. {testable rule that must never be violated}
2. ...

---

## 7. API Overview (optional)

{endpoint listing — methods and paths only, no bodies}
```

## quality criteria

your output is valid when:
- every section narrows the solution space — vague platitudes like "use best practices" are banned.
- non-scope lists at least 5 concrete features excluded from v1.
- conventions are exact (copy-pasteable patterns, not descriptions).
- invariants are testable (you could write an assertion for each).
- nothing in the document is specific to one slice or one pr (that belongs in l2).
- decision test passes for every item: changing it would require changing most of the codebase.

## anti-patterns

- **vague constitution**: "we're building a fast, reliable tool using modern best practices" constrains nothing.
- **over-specific constitution**: listing database table schemas or api request bodies. those belong in l2.
- **missing non-scope**: without explicit exclusions, scope creeps invisibly and llms hallucinate features.

## upstream context

when invoking this skill, include:
- the project idea (problem + solution + target users).
- known technical constraints (team, budget, infra).
- any prior art or existing systems being replaced.

see also: [l0 example](./examples/bookmark-manager/constitution.md) | next: [l1: slice roadmap](./L1-slice-roadmap.md) | [all skills](./README.md)
