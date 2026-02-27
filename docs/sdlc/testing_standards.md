# Agency Testing Standards

> Normative target-state document for tests in this repo. This describes the desired steady state, not necessarily the current state. All new tests must conform. Existing tests should be migrated when touched or when a planned cleanup PR explicitly owns them.

## 1. Philosophy

Tests exist to verify behavior, not implementation. Tests are the primary verification gate — specs give direction, but passing tests prove the code works.

- If a test breaks when internals are refactored but observable behavior is unchanged, the test is wrong.
- Tests are contracts: they document what the system promises to users and operators, not how the code is arranged internally.
- A passing test suite should mean "the product works." A failing test should mean "something meaningful regressed."
- Prefer fewer, higher-confidence tests over many shallow tests. One real integration test with a temp git repo is worth many unit tests that mock away the interesting parts.
- Use red/green TDD: write failing tests from acceptance criteria first (red), then write code to make them pass (green). This prevents both non-functional code and unnecessary code.
- When starting a session, run `make check` first. This reveals project scope, surfaces pre-existing failures, and establishes a testing mindset.

## 2. Scope and Definitions

This document covers the Go codebase: CLI commands (`cmd/agency`), daemon server (`internal/daemon/`), core packages (`internal/`), and end-to-end tests (`internal/commands/`).

Definitions used throughout this document:

- `behavior`: observable CLI output, exit codes, HTTP responses from the daemon API, persisted filesystem state (store, events, checkpoints), git side effects (branches, commits, worktrees), and user-visible error codes.
- `implementation`: internal helper calls, struct field layout, package wiring, goroutine scheduling, or internal method call order.
- `internal boundary`: code owned by this repo (for example `internal/daemon`, `internal/store`, `internal/git`, `internal/exec`).
- `external boundary`: third-party systems or APIs outside this repo (for example GitHub API via `gh` CLI, tmux sessions, external SaaS services).
- `real infrastructure`: real temp git repos, real filesystem via `t.TempDir()`, real `httptest.Server` instances, real process execution — not mocked equivalents.

## 3. Testing Trophy

```text
         /       E2E (real binary)       \      <- SMALLEST layer: real CLI binary,
        /----------------------------------\      real GitHub API (gated, opt-in)
       /   Integration (real infra, httptest) \  <- LARGEST layer: real fs, temp git
      /----------------------------------------\    repos, httptest servers, store
     /        Unit (pure logic, no I/O)         \ <- Pure functions, parsers, validators
    /--------------------------------------------\
   /            Static Analysis                   \ <- go vet, golangci-lint, gofmt
  /------------------------------------------------\
```

Integration tests are the backbone (~60-70%). They use real filesystem, real temp git repos, real HTTP servers, and real store implementations. This is the right shape for a CLI/daemon project because:

1. Most interesting behavior crosses package boundaries (CLI -> daemon -> store -> git -> filesystem).
2. Unit tests for delegation-heavy code provide little confidence.
3. Real infrastructure (git, filesystem, httptest) is fast enough for CI and provides high-fidelity signal.

Unit tests (~20-30%) cover pure logic with meaningful branching — state machines, parsers, validators, error classification. If a function just delegates to dependencies, skip the unit test; integration tests will cover it.

E2E tests (~5-10%) are smoke tests for critical user paths. Build the real binary, invoke it, verify output and side effects. GitHub-backed E2E is gated and opt-in.

## 4. Test Tiers

### Tier 0: Static Analysis

- `gofmt` (formatting)
- `go vet` (correctness)
- `golangci-lint` (comprehensive linting)

Rules:

- Runs on every PR and in local verification commands (`make check`).
- Treat static-analysis failures as real failures (no `|| true`).

### Tier 1: Unit Tests

What belongs here:

- Pure functions (text processing, slug normalization, ID generation, path manipulation)
- Schema validation and version checks
- Configuration parsing
- State machine transitions (status derivation, attention flags)
- Error classification and code mapping

What does not belong here:

- Anything that touches the filesystem
- Anything that requires a git repo
- HTTP handler testing
- Anything that requires mocks to execute — move the test up to integration

Rules:

- No filesystem I/O, no network I/O, no git operations.
- No mocks by default; move the test up a tier if behavior depends on external interactions.
- Keep tests fast and deterministic.

### Tier 2: Integration Tests

This is the primary test tier. The workhorse.

What belongs here:

- Daemon HTTP handler tests via `httptest.NewRequest` + `httptest.NewRecorder`
- Store operations (read/write/lock) against real `t.TempDir()` filesystems
- Git operations against real temp git repos
- Multi-step workflows (checkpoint creation, landing, event recording)
- Command implementations that orchestrate multiple packages
- Error paths that involve filesystem state, store state, or git state

Rules:

- Use real filesystem via `t.TempDir()` — every test gets its own temp dir.
- Use real temp git repos initialized with `testutil.SetupGitRepo(t)`.
- Use `httptest.Server` / `httptest.NewRecorder` for daemon API testing.
- Mock only external boundaries (Section 6).
- Assert through observable behavior: HTTP responses, filesystem state, git state, exit codes — not internal struct fields or call order.

### Tier 3: E2E Tests

What belongs here:

- Full CLI binary invocations (build `agency`, run as subprocess)
- GitHub-backed flows (push, merge, PR creation)
- Cross-process workflows (CLI -> daemon over Unix socket)

Rules:

- Gate with environment variables: `AGENCY_GH_E2E=1` and `GH_TOKEN`.
- Keep E2E in `*_e2e_test.go` files.
- Build the real binary before running: `go build -o <tmpdir>/agency ./cmd/agency`.
- Assert on exit codes, stdout/stderr content, and filesystem side effects.
- Use `testing.Short()` to skip in short mode.
- Tests must be independent and can run with `-count=1`.

## 5. Assertion Standards

### Assert Through Observable Behavior

Prefer behavioral assertions over internal state inspection.

```go
// WRONG: Inspecting internal struct fields after an API call
result, err := service.Land(ctx, opts)
require.NoError(t, err)
assert.True(t, result.internal.cherryPickUsed) // implementation detail

// RIGHT: Assert the behavior contract
result, err := service.Land(ctx, opts)
require.NoError(t, err)
assert.Equal(t, "merged", result.Strategy)
assert.Equal(t, 3, result.CommitCount)
```

For daemon API tests, assert through HTTP responses:

```go
// WRONG: Reaching into handler internals
handler.handleLand(w, r)
assert.True(t, handler.landCalled)

// RIGHT: Assert the HTTP contract
rec := httptest.NewRecorder()
handler.ServeHTTP(rec, req)
assert.Equal(t, http.StatusOK, rec.Code)

var resp LandResponse
require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
assert.Equal(t, "merged", resp.Strategy)
```

For filesystem-backed operations, assert through the store or filesystem:

```go
// RIGHT: Verify events were written
entries, err := os.ReadDir(eventsDir)
require.NoError(t, err)
assert.Len(t, entries, 1)

data, err := os.ReadFile(filepath.Join(eventsDir, entries[0].Name()))
require.NoError(t, err)
assert.Contains(t, string(data), `"event":"worktree.created"`)
```

### Use require for Preconditions, assert for Checks

```go
result, err := service.Land(ctx, opts)
require.NoError(t, err)                         // halt: no point continuing if this fails
assert.Equal(t, "merged", result.Strategy)       // check: show all failures
assert.Equal(t, 3, result.CommitCount)           // check: show all failures
```

- `require` for preconditions that should halt the test. Always use `require.NoError(t, err)` before accessing a result.
- `assert` for checks where you want to see all failures in a single run.

### Error Path Assertions Must Be Typed

For every error code defined in the system, write at least one test that triggers it and asserts the correct code:

```go
t.Run("invocation still running", func(t *testing.T) {
    // Setup: create invocation in running state
    // Act: attempt to land
    // Assert: get EInvocationStillRunning error
    ae, ok := errors.AsAgencyError(err)
    require.True(t, ok)
    assert.Equal(t, errors.EInvocationStillRunning, ae.Code)
})
```

Also assert side effects of error paths: events emitted, cleanup performed, state left unchanged.

## 6. Mocking Policy

### The Cardinal Rule

Use real implementations. Use real temp git repos, real `httptest.Server` instances, real filesystem via `t.TempDir()`. Only introduce an interface+fake when the real thing is impossible. When you must fake something, hand-write it — never use mock generation frameworks.

### Allowed Fakes (External Boundaries Only)

| Boundary | Tool / Pattern | Why |
|---|---|---|
| Process execution (`os/exec`) | `testutil.FakeCommandRunner` | Avoid spawning real `gh`, `tmux`, or other external CLIs in unit/integration tests |
| Tmux sessions | `testutil.FakeTmux` | Tmux is an external process manager; real tmux is impractical in CI |
| GitHub API (via `gh` CLI) | `FakeCommandRunner` with canned responses | External SaaS dependency, rate limits, auth requirements |
| External SaaS APIs | HTTP-level fakes or `FakeCommandRunner` | Third-party cost, nondeterminism, rate limits |

### Disallowed Mocks (Internal Boundaries)

| Thing | Why |
|---|---|
| Filesystem / `os` operations | `t.TempDir()` provides real, isolated filesystem — use it |
| Git operations | Real temp git repos are fast and catch real integration bugs |
| Store reads/writes | The store is part of the behavior contract; real filesystem tests it properly |
| Daemon HTTP handlers | `httptest` is fast and tests real routing/serialization |
| `internal/exec.CommandRunner` in integration tests that own the command | If you control the command, run it for real |
| Mock generation frameworks (`gomock`, `mockery`, etc.) | Hand-written fakes are simpler, more readable, and do not couple tests to interface signatures |

### Exceptions (Temporary and Explicit)

Short-term exceptions are allowed only when migration work is in progress and the test would otherwise be deleted or blocked. Requirements:

- The exception must be documented in the PR description or a code comment at the mock site.
- The exception must be time-bounded (for example, "remove in this PR before merge" or "remove in next planned cleanup PR").
- The exception must not be hidden in global/shared test setup.
- Every exception entry must name the intended replacement layer/test.

## 7. Data Setup and Infrastructure

### Temp Git Repos

Create real git repos for testing. Use the shared helper:

```go
repoDir := testutil.SetupGitRepo(t)
```

This creates a temp dir, initializes a git repo with an initial commit, and cleans up automatically. Always create repo state programmatically. Do not check fixture repos into `testdata/`.

For tests that need specific git state (branches, commits, files), build it on top of the base repo:

```go
repoDir := testutil.SetupGitRepo(t)
// Add a file and commit
os.WriteFile(filepath.Join(repoDir, "hello.txt"), []byte("world"), 0o644)
run(t, repoDir, "git", "add", ".")
run(t, repoDir, "git", "commit", "-m", "add hello")
```

### In-Process HTTP Servers

For daemon handler integration tests, use `httptest`:

```go
func setupTestServer(t *testing.T) (*httptest.Server, *Client) {
    t.Helper()
    handler := NewRouter(realDeps...)
    srv := httptest.NewServer(handler)
    t.Cleanup(srv.Close)
    client := NewClient(srv.URL)
    return srv, client
}
```

Never start the compiled binary for integration tests. That is E2E territory.

### Hermetic Git Environment

Use `testutil.HermeticGitEnv(t)` to ensure tests don't leak or depend on the host's git configuration.

### Cleanup

Every test resource must be cleaned up via `t.Cleanup()` or `t.TempDir()` (which self-cleans). Never require manual teardown. Prefer `t.Cleanup` for resource lifetimes that must survive `t.FailNow()`.

## 8. Test Organization

### File Layout

```text
internal/
|- <package>/
|  |- service.go
|  |- service_test.go          # tests mirror source files 1:1
|  |- helpers_test.go          # package-local test helpers
|- testutil/
|  |- fake_runner.go           # shared fake: FakeCommandRunner
|  |- fake_tmux.go             # shared fake: FakeTmux
|  |- setup.go                 # shared setup: SetupGitRepo, HermeticGitEnv
|- commands/
|  |- gh_e2e_test.go           # E2E tests (gated)
```

Expectations:

- One test file per source file: `service.go` -> `service_test.go`.
- Test helpers specific to one package go in `helpers_test.go` within that package.
- Test helpers shared across packages go in `internal/testutil/`.
- Do not split tests by unit/integration file names. Use build-tagged files only when required (e.g., E2E).
- Golden files live in `testdata/` next to the test file.

### Discovering Existing Helpers

Before writing new test helpers, check:

1. `internal/testutil/` for shared helpers.
2. `helpers_test.go` in the current package.
3. Neighboring `_test.go` files for patterns already in use.

Reuse existing helpers when they align with this guide. If they contradict this guide, follow this guide and refactor the helper if practical.

## 9. Naming

### Test Function Names

Use `Test<Function>_<Scenario>` or `Test<Type>_<Method>_<Scenario>`:

```go
TestLand_Success
TestLand_NothingToLand
TestLand_ConflictDuringCherryPick
TestHandleLandRequest_InvalidBody
TestHandleLandRequest_InvocationRunning
```

Scenarios describe the condition or outcome, not implementation steps. Do not prefix with `TestUnit_` or `TestIntegration_`.

### Table-Driven Test Names

Table case names should be concise and describe the scenario:

```go
tests := []struct {
    name     string
    // ...
}{
    {"valid input", ...},
    {"missing field", ...},
    {"empty slug rejects", ...},
}
```

### When to Use Table-Driven Tests

Use table-driven tests when you have 3+ cases testing the same code path with different inputs. For tests with complex setup, unique assertions, or different dependency configurations, use standalone test functions. Do not force table-driven when cases are structurally dissimilar — a table with `setupFn func()` fields has gone too far.

## 10. Determinism and Isolation

### Time

- Never use real `time.Sleep` in unit or integration tests. Inject a sleeper/clock or advance a fake time source.
- Avoid `time.Now()` in assertions; inject time or compare within explicit bounds.
- If randomness is required, fix the seed and assert on invariants.
- For async behavior, use `require.Eventually`/`assert.Eventually` with tight, bounded timeouts.

### Parallelism

- Default to `t.Parallel()` for tests that use isolated state (their own temp dir, their own server).
- Tests that touch environment variables, package-level globals, or shared resources must NOT call `t.Parallel()`.
- Every test gets its own `t.TempDir()`. Never share filesystem state between tests.
- Never rely on test execution order.
- If adding `t.Parallel()` breaks a test, the test has a design problem — fix the test.

### Environment and Globals

- Use `t.Setenv` for environment variables. Always restore package-level globals with `t.Cleanup`.
- Tests that mutate env/globals must not run in parallel.
- Tests must not require `gh`, `tmux`, or network access. Use fakes or helper scripts instead.
- Real `git` is acceptable for integration tests that create temp repos.

## 11. CI and Local Commands

### Local Commands

```makefile
make check       # fast checks: fmt-check + lint + test + build
make verify      # full checks: fmt-check + lint + mod-tidy + race + e2e + completions + build
make test        # go test ./...
make test-race   # go test -race -count=1 ./...
make lint        # golangci-lint run
make fmt-check   # gofmt formatting check
make e2e         # GitHub E2E (requires GH_TOKEN, AGENCY_GH_E2E=1)
```

Command semantics:

- `make check`: fast local feedback loop for routine development (static checks + tests + build, no race detector, no E2E).
- `make verify`: full verification before merge (everything including race detector, E2E, and completions).
- `make e2e`: explicit GitHub-backed E2E (requires `GH_TOKEN`; used selectively, not in every CI run).

### CI Shape

1. `go test ./...` on every push and PR.
2. E2E job runs conditionally when `AGENCY_GH_TOKEN` secret is configured.
3. E2E runs a single targeted test (`TestGHE2EPushMerge`) with `-count=1`.

## 12. Golden File / Snapshot Tests

Use golden files for:

- CLI output formatting (help text, status output, structured error messages).
- Serialized API responses where hand-writing assertions is tedious.

Do not use golden files for output containing timestamps, random IDs, or non-deterministic ordering. Avoid golden files for CLI help text when it changes frequently; prefer substring/regex assertions for stable parts.

Convention:

- Golden files live in `testdata/` next to the test file.
- Update with an `-update` flag: `go test -run TestFoo -update`.

```go
var update = flag.Bool("update", false, "update golden files")

func TestOutput(t *testing.T) {
    got := runCommand(...)
    golden := filepath.Join("testdata", t.Name()+".golden")
    if *update {
        os.WriteFile(golden, got, 0o644)
    }
    expected, _ := os.ReadFile(golden)
    assert.Equal(t, string(expected), string(got))
}
```

## 13. What Not to Test

- Library internals (for example, `testify` internals, `cobra` internals, `fsnotify` internals).
- Framework behavior already guaranteed by the framework (unless you are testing your integration with it).
- Trivial getters/setters that just return a field.
- Constructor wiring (`NewService(dep1, dep2)` that just assigns fields).
- Exact log messages (too brittle; test the behavior that triggers logging, not the string).
- Code with no branching logic and no meaningful failure modes.

## 14. Binding Requirements

These requirements are enforced by CLAUDE.md and must be tested:

- Every new error code must have at least one test that triggers it and asserts the correct code.
- Every contract change (schema versions, event formats) must have tests that reject old or unknown versions.
- Every flow that writes events must test both success and event-write failure paths.
- Process execution must go through `internal/exec` — tests should verify no `os/exec` leaks.
- Safe delete operations must use `fs.SafeRemoveAll` — tests should verify containment checks.
- Path comparisons must use absolute, clean, symlink-resolved paths.

## 15. Migration Rules for Existing Tests

When modifying existing tests during cleanup:

1. Prefer replacement over patching brittle mocks.
2. If deleting a test, identify the replacement layer (unit, integration, or E2E).
3. Do not add new global test mocks.
4. Do not introduce mock generation frameworks.
5. If a temporary exception is required, document it in the PR description and remove it before merge when feasible.

## 16. Pre-Submission Checklist

Before considering tests complete:

- [ ] All new/changed logic has test coverage for happy path AND error paths.
- [ ] Tests pass with `go test -race ./...`.
- [ ] Tests pass under `make verify`.
- [ ] `t.Parallel()` is used for isolated tests; no parallel tests mutate env/globals.
- [ ] No mocks exist where a real implementation is feasible.
- [ ] No tests assert on implementation details (internal call order, private state).
- [ ] Test names describe conditions/outcomes, not implementation steps.
- [ ] Error codes are tested with typed assertions (`errors.AsAgencyError`).
- [ ] Event-writing flows test both success and append-failure paths.

## References

- `.claude/prompts/test-writing.md` — detailed test-writing guide for agents
- `docs/testing.md` — testing policy and coverage inventory
- `CLAUDE.md` — binding rules enforced in code and tests
- `docs/contracts/events.md` — event schema and write contract
- `internal/testutil/` — shared test helpers (FakeCommandRunner, FakeTmux, SetupGitRepo)
