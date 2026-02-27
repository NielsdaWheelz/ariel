# L4: PR Brief

> scope one pr so the implementing agent knows what to build. brief enough that a human reads it critically before approving.

## identity

you are writing a short brief for one pr. this is the last document before code — it must be specific enough to give direction but short enough that the human actually reads it.

## input

you receive:
- **l2 slice spec**: acceptance criteria and key decisions.
- **l3 pr roadmap entry**: goal, dependencies, acceptance.
- **current codebase state**: for understanding what exists.

## output

you produce: one **pr brief** file: `{slice}_pr{nn}.md`.

target length: 20-40 lines.

## process

1. read the l3 pr roadmap entry and relevant l2 acceptance criteria.
2. **scout the codebase**: explore the files this pr will touch. look for:
   - existing patterns the implementation should follow (routing, service structure, test setup).
   - hidden dependencies or shared state that could be affected.
   - related tests that reveal expected behavior.
   - anything surprising that might change the approach.
   this is cheap reconnaissance — find where the sticky bits are before writing the brief, not after.
3. write the brief: goal, builds on, acceptance, non-goals, key decisions (if any).

## what goes in the brief

- **goal**: one sentence.
- **builds on**: what must be merged first.
- **acceptance**: what's true when done (observable behaviors). reference l2 acceptance criteria where applicable.
- **non-goals**: what this pr does NOT do.
- **key decisions**: only if this pr forces decisions not already in l2. brief.

## what does NOT go in the brief

- file paths, function signatures, class names.
- test code or test file paths.
- implementation-step sequences.
- full api request/response bodies (reference l2 if needed).
- traceability matrices or decision ledgers.

## template

```markdown
# PR-{NN}: {name}

## Goal

{one sentence}

## Builds On

{what must be merged first, or "nothing — first pr in slice"}

## Acceptance

- {observable behavior}
- {observable behavior}
- ...

## Non-Goals

- does not {out-of-scope behavior}
- ...

## Key Decisions (if any beyond L2)

**{topic}**: {brief decision}
```

## quality criteria

your output is valid when:
- a senior engineer could read it in 2 minutes and know what to build.
- acceptance bullets are observable and testable.
- non-goals prevent scope creep.
- no implementation detail that the agent should decide by reading the codebase.
- total length is 20-40 lines.

## anti-patterns

- **scope smuggling**: adding work not required by the l3 entry or l2 acceptance criteria.
- **implementation dictation**: specifying how to build, not what to build. the agent reads the codebase.
- **brief inflation**: if the brief exceeds 40 lines, you are over-specifying.

## implementation guidance

once the brief is approved, the implementing agent:
1. **first, run the existing tests.** this forces discovery of the test suite, reveals project scope, and establishes a testing mindset for the session.
2. reads the brief + l2 spec + codebase.
3. writes failing tests from acceptance criteria (red). include rich detail in assertion messages — extra context in failures helps the agent self-correct.
4. writes code to make tests pass (green).
5. submits for code review with manual test evidence (terminal output or screenshots).

if stuck after two failed correction attempts, **start a fresh session**. accumulated context from failed approaches makes agents progressively worse. treat it as a fresh start with a better initial prompt, not an iteration on broken context.

## upstream context

when writing a pr brief, include:
- l2 slice spec (relevant acceptance criteria and key decisions).
- l3 pr roadmap entry for this pr.
- current codebase state.

see also: [l4 example](./examples/bookmark-manager/slice-2-pr01.md) | [all skills](./README.md)
