---
name: test-writing
description: Codebase-agnostic testing standards. Use when writing tests, reviewing test quality, or setting up testing conventions for a project.
disable-model-invocation: true
---

# Test Writing Guide

> Codebase-agnostic testing standards template. Adapt the tiers, tooling, and examples to your project's language, framework, and architecture. The philosophy and structural rules are universal.

## 1. Philosophy

Tests exist to verify behavior, not implementation. Tests are the primary verification gate — specs give direction, but passing tests prove the code works.

- If a test breaks when internals are refactored but observable behavior is unchanged, the test is wrong.
- Tests are contracts: they document what the system promises to users and operators, not how the code is arranged internally.
- A passing test suite should mean "the product works." A failing test should mean "something meaningful regressed."
- Prefer fewer, higher-confidence tests over many shallow tests. One real integration test is worth many unit tests that mock away the interesting parts.
- Use red/green TDD: write failing tests from acceptance criteria first (red), then write code to make them pass (green).
- When starting a session, run the existing test suite first. This reveals project scope, surfaces pre-existing failures, and establishes a testing mindset.

## 2. Deciding What to Test

### Test This

- Every public function with non-trivial logic.
- Every error code/type the system defines. If an error exists, a test must trigger it.
- Error paths with equal rigor to happy paths. Every error branch that wraps, emits, or cleans up deserves a test case.
- Edge cases where logic branches: zero values, empty collections, nil/null inputs, boundary conditions.
- The contract your code provides: given this input/state, expect this output/side effect.

### Do NOT Test This

- Trivial getters/setters that just return a field.
- Constructor wiring that just assigns fields.
- Third-party library behavior already guaranteed by the library.
- Exact log messages (too brittle; test the behavior that triggers logging, not the string).
- Implementation details like internal method call order.
- Code with no branching logic and no meaningful failure modes.

## 3. Testing Trophy

The testing trophy inverts the traditional pyramid. The largest layer depends on your architecture:

```text
         /          E2E            \      <- Real user flows, real stack
        /----------------------------\
       /        Integration           \   <- Real infrastructure, real I/O
      /--------------------------------\
     /           Unit                   \ <- Pure logic, no I/O
    /------------------------------------\
   /         Static Analysis              \ <- Types, linting, formatting
  /----------------------------------------\
```

**Choose your largest layer based on project type:**

| Project Type | Largest Layer | Why |
|---|---|---|
| CLI / daemon / backend service | Integration (~60-70%) | Most behavior crosses package boundaries; real I/O is fast enough |
| SSR web app (Next.js, Rails, etc.) | E2E (~40-50%) | SSR integration tests require excessive mocking; real browser tests catch more |
| Pure library / SDK | Unit (~60-70%) | Pure logic with clear input/output contracts |
| API service with DB | Integration (~60-70%) | Real DB, real HTTP; mock only external services |
| Frontend SPA | Component + E2E (~60%) | Real browser rendering; mock only network boundaries |

The remaining layers fill the gaps. Every project needs all layers — the question is proportion.

## 4. Test Tiers

### Tier 0: Static Analysis

Language-appropriate static checks: type checking, linting, formatting.

Rules:

- Runs on every PR and in local verification commands.
- Treat static-analysis failures as real failures (no suppression in CI).

### Tier 1: Unit Tests

What belongs here:

- Pure functions (parsing, normalization, computation, serialization)
- Schema validation and version checks
- Configuration parsing
- State machine transitions
- Error classification and code mapping

What does not belong here:

- Anything that touches the filesystem, network, or database
- Anything that requires mocks to execute — move it up a tier

Rules:

- No I/O of any kind.
- No mocks by default; if the test needs a mock to run, it belongs in integration.
- Keep tests fast and deterministic.

### Tier 2: Integration Tests

The workhorse for most projects. Uses real infrastructure that is fast and local.

What belongs here:

- HTTP handler/endpoint tests against real (in-process) servers
- Database-backed workflows against real (local) databases
- Filesystem operations against real temp directories
- Multi-step workflows that cross package/module boundaries
- Error paths that involve I/O state

Rules:

- Use real, isolated infrastructure (temp dirs, test databases, in-process servers).
- Mock only external boundaries (see Mocking Policy).
- Assert through observable behavior: responses, filesystem state, database state — not internal fields or call order.

### Tier 3: E2E Tests

What belongs here:

- Full application invocations (real binary, real browser, real server)
- Cross-process or cross-service workflows
- Authentication and session flows
- Flows that depend on deployment topology

Rules:

- Gate expensive E2E behind environment variables or flags.
- Keep E2E count small — cover critical user journeys, not every permutation.
- Tests must be independent and parallelizable.
- Seed data through app APIs or dedicated scripts, not direct DB manipulation from tests.

## 5. Assertion Standards

### Assert Through Observable Behavior

Prefer behavioral assertions over internal state inspection:

- **API tests**: assert on HTTP status codes, response bodies, headers.
- **CLI tests**: assert on exit codes, stdout/stderr content, filesystem side effects.
- **UI tests**: assert on rendered state, navigation outcomes, accessibility attributes.
- **Service tests**: assert on return values, persisted state readable through public APIs.

Avoid: asserting on private fields, internal call counts, mock invocation order.

### Preconditions vs. Checks

Use two assertion styles:

- **Halting assertions** for preconditions that make the rest of the test meaningless if they fail. (`require` in Go/testify, early `assert` + return in other frameworks, or framework-specific `fail-fast` variants.)
- **Collecting assertions** for checks where you want to see all failures in a single run.

### Error Path Assertions Must Be Typed

When your codebase defines error codes or typed errors, assert on the specific type/code — not just "an error occurred":

```
// WEAK: just checks for any error
assert error != nil

// STRONG: checks the specific error contract
assert error.code == "CONFLICT"
assert error.type == ConflictError
```

Also assert side effects of error paths: cleanup performed, state left unchanged, events/logs emitted.

## 6. Mocking Policy

### The Cardinal Rule

Use real implementations by default. Only introduce a fake/mock/stub when the real thing is genuinely impractical (external SaaS APIs, paid services, non-deterministic systems, truly slow resources).

### Allowed Mocks (External Boundaries)

Mock at the boundary between your code and things you do not own or control:

- External SaaS APIs (payment providers, LLM APIs, auth providers)
- External process managers or system services not available in CI
- Third-party services with cost, rate limits, or nondeterminism

Prefer HTTP-level or network-level fakes over module/function-level mocks. The closer the fake is to the real boundary, the more it tests.

### Disallowed Mocks (Internal Boundaries)

Do not mock things you own:

- Your own database — use a real test instance
- Your own filesystem — use temp directories
- Your own HTTP handlers — use in-process test servers
- Your own modules/packages/services — if you need to mock an internal to test something, the test belongs at a higher tier
- Internal function calls — this couples tests to implementation

### Mock Frameworks

Prefer hand-written fakes over generated mocks. Hand-written fakes are simpler, more readable, and do not couple tests to interface signatures. If a fake is complex enough to need generation, consider whether the test belongs at a higher tier.

### Exceptions (Temporary and Explicit)

When a mock exception is unavoidable:

- Document it in the PR description or a code comment at the mock site.
- Make it time-bounded ("remove before merge" or "remove in cleanup PR").
- Do not hide it in global/shared test setup.
- Name the intended replacement layer/test.

## 7. Data Setup and Infrastructure

### Isolation

Every test gets its own isolated state. Use language-appropriate mechanisms:

- Temp directories (cleaned automatically by the test framework)
- Test databases (per-test schemas, transactions that roll back, or ephemeral instances)
- In-process servers (started and stopped per test or per suite)

Never share mutable state between tests. Never rely on test execution order.

### Programmatic Setup

Create test state programmatically, not from checked-in fixtures:

- Build database state through ORM/model factories, not raw SQL dumps.
- Build filesystem state through code, not fixture directories.
- Build git state through commands, not pre-built repos.

Exceptions: golden files for output comparison, static config files for parsing tests.

### Cleanup

Every test resource must be cleaned up automatically. Never require manual teardown. Use framework-provided lifecycle hooks (cleanup callbacks, after-each, etc.).

## 8. Test Organization

### File Layout

Mirror source files 1:1 with test files. Keep test helpers close to where they are used:

- **Package-local helpers**: in a helpers/utils test file within the package.
- **Shared helpers**: in a dedicated test utilities package/module.
- **Golden files**: in a `testdata/` or `__snapshots__/` directory next to the test.

### Discovering Existing Helpers

Before writing new test helpers, check:

1. Shared test utility packages.
2. Helper files in the current package.
3. Neighboring test files for patterns already in use.

Reuse existing helpers. Do not duplicate.

## 9. Naming

### Test Names Describe Scenarios

Name tests by condition and outcome, not by implementation steps:

```
// GOOD: describes the scenario
TestLand_ConflictDuringCherryPick
test_create_library_with_duplicate_name_returns_409
it("shows error message when API returns 500")

// BAD: describes implementation
TestLand_CallsCherryPickThenHandlesError
test_create_library_calls_service_and_catches_exception
it("calls onError callback with error object")
```

Do not prefix with `TestUnit_` or `TestIntegration_`.

### Table-Driven / Parameterized Tests

Use parameterized tests when you have 3+ cases testing the same code path with different inputs. Do not force parameterization when cases have different setup, different assertions, or structurally dissimilar logic.

## 10. Determinism and Isolation

### Time

- Never use real sleeps for synchronization. Inject clocks/sleepers or use bounded polling/waits.
- Avoid wall-clock time in assertions. Inject time or compare within explicit bounds.
- If randomness is required, fix the seed and assert on invariants.

### Parallelism

- Default to parallel execution for tests with isolated state.
- Tests that touch environment variables, globals, or shared resources must not run in parallel.
- Never rely on test execution order.
- If enabling parallelism breaks a test, the test has a design problem — fix the test.

### Environment and External Dependencies

- Isolate environment variable mutations per test. Restore after each test.
- Tests must not require external tools, network access, or services unless gated behind an opt-in flag.
- Real local tools (git, compilers) are acceptable for integration tests when they are reliably available.

## 11. CI and Local Commands

Define at minimum these verification tiers:

| Command | Scope | When |
|---|---|---|
| Fast check | Static analysis + unit tests + build | Every save / pre-push |
| Full verify | All tests including integration, race/thread-safety checks | Before merge |
| E2E | Full stack E2E (may require secrets/services) | Before merge / in CI |

Command semantics should be documented in the project's Makefile, package.json, or equivalent.

## 12. Golden File / Snapshot Tests

Use golden files for:

- CLI output formatting, structured error messages.
- Serialized API responses where hand-writing assertions is tedious.

Do not use golden files for output containing timestamps, random IDs, or non-deterministic ordering.

Convention:

- Golden files live next to the test file (in `testdata/`, `__snapshots__/`, etc.).
- Provide an update mechanism (flag, env var, or command) to regenerate.

## 13. Migration Rules for Existing Tests

When modifying existing tests during cleanup:

1. Prefer replacement over patching brittle mocks.
2. If deleting a test, identify the replacement tier (unit, integration, or E2E).
3. Do not add new global test mocks.
4. Do not introduce mock generation frameworks.
5. If a temporary exception is required, document it and remove it before merge when feasible.

## 14. Pre-Submission Checklist

Before considering tests complete:

- [ ] All new/changed logic has test coverage for happy path AND error paths.
- [ ] Tests pass with thread-safety / race detection enabled.
- [ ] Tests pass under the full verification command.
- [ ] Parallel-safe tests are marked parallel; env/global-mutating tests are not.
- [ ] No mocks exist where a real implementation is feasible.
- [ ] No tests assert on implementation details (internal call order, private state).
- [ ] Test names describe conditions/outcomes, not implementation steps.
- [ ] Error codes/types are tested with typed assertions.
