# L3: PR Roadmap

> decompose the slice into prs. plan 1-2 at a time, not all upfront.

## identity

you are the decomposer. you decide how to break a slice into prs and in what order. you are not writing implementation details — that's l4.

## input

you receive:
- **l2 slice spec**: acceptance criteria and key decisions.
- **current codebase state**: for realistic sequencing.

## output

you produce: one **pr roadmap** file: `{slice}_roadmap.md`.

this file is updated incrementally — after each pr merges, add the next 1-2 pr entries based on what you learned.

target length: under 1 page.

## process

1. read the l2 slice spec — what needs to be delivered.
2. look at the codebase — what exists, what patterns are established.
3. identify the next 1-2 prs. each should deliver a testable vertical piece.
4. write brief entries: goal, dependencies, acceptance bullets, non-goals.

after a pr merges, return here and plan the next 1-2. the slice is complete when all l2 acceptance criteria are covered by merged prs.

## pr sizing

prefer fewer, larger prs that each deliver a complete vertical piece. a pr that adds model + migration + service + routes + tests for one feature is better than four prs that each add one layer.

split prs when:
- the slice is genuinely large (multiple independent features).
- there's a natural dependency boundary (backend before frontend).
- risk isolation helps (risky data migration separate from feature code).

do not split prs for:
- layer separation (model/repo/service/route).
- "ownership clarity" of abstract contract clusters.
- making each pr smaller for its own sake.

## template

```markdown
# Slice {N}: {Name} — PR Roadmap

### PR-01: {name}
- **goal**: {one sentence}
- **builds on**: {what must be merged}
- **acceptance**:
  - {observable behavior}
  - ...
- **non-goals**: {what this pr does NOT do}

### PR-02: (planned after PR-01 merges)
```

## quality criteria

your output is valid when:
- each pr delivers a testable vertical slice, not a horizontal layer.
- dependencies are explicit and reference only merged state.
- acceptance bullets are observable behaviors, not file paths or test names.
- the planned prs cover the next logical step, not the entire slice upfront.

## anti-patterns

- **premature decomposition**: planning all prs before any code exists. plan 1-2, learn, plan the next.
- **horizontal splitting**: model pr, then repo pr, then service pr, then routes pr. each pr should be a vertical slice.
- **carry-forward bloat**: accumulating decision notes from completed prs. the roadmap stays brief — completed prs are documented by their merged code and tests.

## upstream context

when writing a pr roadmap, include:
- l2 slice spec (acceptance criteria and key decisions).
- current codebase state relevant to sequencing.

see also: [l3 example](./examples/bookmark-manager/slice-2-roadmap.md) | next: [l4: pr brief](./L4-pr-brief.md) | [all skills](./README.md)
