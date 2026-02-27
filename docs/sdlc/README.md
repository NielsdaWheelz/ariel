# Software Development Lifecycle

> specs give direction. code + tests prove correctness.

## hierarchy

```
┌──────────────────────────────────────────────────┐
│  l0  constitution     system-wide constraints     │
└─────────────────────┬────────────────────────────┘
                      ▼
      ┌──────────────────────────────────┐
      │  l1  slice roadmap   ordering    │
      └──────────────┬───────────────────┘
                     ▼
           ┌────────────────────┐
           │  l2  slice spec    │
           │  (acceptance +     │
           │   key decisions)   │
           └────────┬───────────┘
                    ▼
           ┌────────────────────┐
           │  l3  pr roadmap    │
           │  (1-2 at a time)   │
           └────────┬───────────┘
                    ▼
           ┌────────────────────┐
           │  l4  pr brief      │
           │  (20-40 lines)     │
           └────────┬───────────┘
                    ▼
              ┌──────────┐
              │   code   │
              │ + tests  │
              └──────────┘
```

l0 defines project constraints. l1 orders the work. l2 defines what the slice delivers. l3 decides pr ordering. l4 scopes one pr. code + tests are the source of truth.

## core rules

- specs are guides, not source of truth. code + passing tests are.
- never silently deviate from acceptance criteria without updating the plan.
- never leave scope boundaries implicit.
- every acceptance criterion must be testable.
- write tests first (red/green TDD). a pr is done when tests pass and code review approves.
- plan 1-2 prs at a time, not all upfront.

## dispatch logic

after each step completes, restart from the top.

```
if no l0 exists                  -> write l0
if no l1 exists                  -> write l1
if next slice has no l2          -> write l2
if l3 missing or next pr unplanned -> write/update l3
if next pr has no l4             -> write l4
if l4 exists and pr not started  -> implement pr (tests first, then code)
if all l2 acceptance criteria met -> slice complete, loop to next slice
```

## verification

the plan is the direction gate. code + tests are the proof gate.

**before implementation**: human reads slice spec + pr brief. short enough to read critically in under 5 minutes. approve direction or redirect.

**during implementation**: agent runs existing tests first to understand the codebase, then writes failing tests from acceptance criteria (red), then writes code to pass them (green).

**after implementation**: human reviews code + tests. this is the real quality gate. every pr must include:
1. **implementation** — a single, focused change.
2. **automated tests** — that fail if the implementation is reverted.
3. **manual test evidence** — terminal output or screenshots proving the feature works. passing tests alone are not enough.
4. **updated docs** — if the change affects user-facing behavior.

the human must understand the code they approve. if you can't explain what it does, don't merge it — that's how cognitive debt accumulates.

## escalation

| trigger | action |
|---|---|
| implementation diverges from brief | update l4 to match reality, note why |
| slice spec needs revision | update l2, adjust l3/l4 |
| l1 ordering is wrong | fix l1, update affected slices |
| l0 constraint is wrong | full stop, revise l0, audit downstream |

divergence is expected. update the docs to match the code, not the other way around.

## when to use this pipeline

the full l0→l4 pipeline is for feature development: new slices, new capabilities.

for smaller work, right-size the process:
- **bug fix in existing slice**: write a brief l4, implement, submit.
- **small refactor**: no spec needed. implement with tests, submit.
- **exploratory spike**: skip specs entirely. build it, evaluate, decide whether to keep or restart with a proper spec.
- **one-off improvement**: l4 brief if scope is ambiguous, otherwise just implement.

the rule: if you can explain the change in one sentence and the scope is obvious, skip to code. if there's ambiguity about what to build or how it affects other parts, write the relevant spec layer.

## skill docs

- [l0: constitution](./L0-constitution.md)
- [l1: slice roadmap](./L1-slice-roadmap.md)
- [l2: slice spec](./L2-slice-spec.md)
- [l3: pr roadmap](./L3-pr-roadmap.md)
- [l4: pr brief](./L4-pr-brief.md)

## examples

worked example (bookmark manager):
- [l0: constitution](./examples/bookmark-manager/constitution.md)
- [l1: roadmap](./examples/bookmark-manager/roadmap.md)
- [l2: slice spec](./examples/bookmark-manager/slice-2-spec.md)
- [l3: pr roadmap](./examples/bookmark-manager/slice-2-roadmap.md)
- [l4: pr brief](./examples/bookmark-manager/slice-2-pr01.md)
