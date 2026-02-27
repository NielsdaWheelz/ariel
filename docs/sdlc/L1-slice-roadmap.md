# L1: Slice Roadmap

> decompose a constitution into an ordered sequence of vertical slices.

## identity

you are the planner. you take the system described in l0 and break it into vertical slices — each delivering user-visible value — ordered by dependencies, risk, and priority.

## input

you receive:
- **l0 constitution**: vision, scope, non-scope, core abstractions, architecture, constraints.

## output

you produce: a **slice roadmap** (markdown) listing every slice with goal, outcome, dependencies, acceptance criteria, and (optionally) risks. target length: 1-2 pages.

## process

1. read the l0 scope list. each scope bullet is a candidate slice.
2. **slice vertically, not horizontally.** each slice delivers a complete path through the stack (data -> logic -> api -> ui), not a single layer.
3. order slices using:
   - **user journey**: follow the user through the product; each milestone is a slice.
   - **risk-first**: put the scariest unknowns early. fail fast.
   - **value-first**: put the most valuable features early.
4. draw the **dependency DAG** — which slices must be completed before others can start.
5. for each slice, write goal, outcome, dependencies, and acceptance criteria.
6. consider a **walking skeleton** for slice 0: the thinnest end-to-end path that proves all pieces connect.
7. verify: the roadmap contains zero technical details (no schemas, no apis, no file paths). those belong in l2.

## template

```markdown
# {Project Name} — Slice Roadmap

## Dependency graph

{ASCII DAG showing slice ordering and parallelism}

## Slices

### Slice 0: {Name}
- **Goal**: {one sentence}
- **Outcome**: {what's true when done}
- **Dependencies**: None
- **Acceptance**: {how we know it's done — observable behaviors}
- **Risks**: {optional — unknowns to investigate}

### Slice 1: {Name}
- **Goal**: {one sentence}
- **Outcome**: {what's true when done}
- **Dependencies**: Slice 0
- **Acceptance**: {observable behaviors}

### Slice N: ...
```

## quality criteria

your output is valid when:
- every slice is vertical (touches multiple layers of the stack, not just one).
- every slice delivers user-visible or developer-testable value.
- dependencies form a DAG (no cycles).
- acceptance criteria are observable (not "works correctly" but "user can do X and sees Y").
- zero technical details (no schemas, endpoints, function names).
- all l0 scope items are covered by at least one slice.
- no l0 non-scope items appear in any slice.

## anti-patterns

- **horizontal slicing**: "phase 1: backend. phase 2: frontend." nothing works until everything works.
- **missing dependencies**: without explicit ordering, slices get built in random order, causing rework.
- **vague acceptance**: "it works" is not acceptance criteria. specify what the user can do and what they observe.

## upstream context

when invoking this skill, include:
- l0 vision, scope, non-scope.
- l0 core abstractions and architecture components.

see also: [l1 example](./examples/bookmark-manager/roadmap.md) | next: [l2: slice spec](./L2-slice-spec.md) | [all skills](./README.md)
