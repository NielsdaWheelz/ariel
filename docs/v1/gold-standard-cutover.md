# Gold Standard Cutover

## Scope

This document defines the hard cutover from the current Discord-first assistant
runtime to the production target:

- OpenAI Responses API.
- Direct Responses function-call execution through Ariel policy.
- First-class Agency capabilities.
- Discord interaction UX for approvals, jobs, and status.
- Regression evals that gate daily use.
- Private-by-default droplet deployment.

Voice is out of scope.

## Cutover Rule

This is a hard cutover.

- Delete Chat Completions support.
- Delete legacy model-provider branches.
- Delete fake production action-proposal formats.
- Do not keep compatibility flags.
- Do not add fallback providers.
- Do not keep old Discord approval commands as a supported path after buttons ship.
- Keep tests only when they exercise the new production path.

Dead code is removed in the same change that replaces it.

## Goals

- A real model turn can call Ariel capabilities in production.
- Every tool call passes schema validation, policy, egress checks, and audit logging.
- Agency code work starts through `cap.agency.*`, not shell commands.
- Discord shows useful controls without exposing Ariel's internal API.
- Evals catch regressions in routing, safety, grounding, Agency, and Discord delivery.
- The droplet can restart without losing conversations, jobs, approvals, memory, or Agency
  state.

## Non-Goals

- No voice.
- No public Ariel API.
- No generic shell, SSH, or arbitrary command capability.
- No inbound MCP control plane.
- No LangGraph or separate orchestration framework.
- No OpenAI Agents SDK in this cutover.
- No provider abstraction.
- No plugin system.
- No public multi-user tenancy.
- No smart-home, payment, or financial transaction automation.

## Key Decisions

### Use direct Responses API

Ariel already owns state, tools, approvals, workers, memory, and audit events. Direct
Responses API integration keeps the control flow visible in `app.py` and avoids a second
runtime loop.

The model settings are:

- `ARIEL_OPENAI_API_KEY`
- `ARIEL_MODEL_NAME`, default `gpt-5.5`
- `ARIEL_MODEL_REASONING_EFFORT`, default `medium`
- `ARIEL_MODEL_VERBOSITY`, default `low`
- `ARIEL_MODEL_TIMEOUT_SECONDS`

Remove:

- `ARIEL_MODEL_PROVIDER`
- `ARIEL_MODEL_API_BASE_URL`
- Chat Completions request code.

### Keep Postgres canonical

OpenAI provider state is not canonical memory.

Use `store: false` for Responses calls. During one turn, keep returned Response items in
memory only as needed to continue function-call output. Persist Ariel turn events, action
attempts, approvals, jobs, artifacts, and memory in Postgres.

Do not persist raw reasoning items.

### Use Responses function calls as the only production tool shape

The model receives tool definitions derived from the Ariel capability registry.

When the model emits a function call:

1. Parse the exact Responses function-call item at the model boundary.
2. Validate the capability id and JSON arguments.
3. Create an action attempt.
4. Run policy.
5. Execute allowlisted reads inline.
6. Create an approval for approval-required actions.
7. Return a function-call output item to the Responses loop.
8. Continue until the model emits final assistant text or a turn budget is hit.

Do not convert function calls into a second internal proposal format first.

### Keep one model loop

The turn loop is linear:

1. Load session and turn state.
2. Build deterministic context.
3. Call Responses.
4. Handle zero or more function calls.
5. Save final assistant text.
6. Capture memory candidates.
7. Emit surface events.
8. Return the Discord/API response.

There is no planner object, no agent graph, no handoff system, and no generic tool router.

### Use Agency daemon API, not shell

Ariel talks to Agency through its local daemon socket.

Required settings:

- `ARIEL_AGENCY_SOCKET_PATH`
- `ARIEL_AGENCY_ALLOWED_REPO_ROOTS`
- `ARIEL_AGENCY_DEFAULT_BASE_BRANCH`
- `ARIEL_AGENCY_DEFAULT_RUNNER`

`ARIEL_AGENCY_ALLOWED_REPO_ROOTS` is mandatory when Agency capabilities are enabled.
Each path is absolute, clean, symlink-resolved, and compared exactly.

### Keep Discord Gateway primary

The bot connects outbound to Discord Gateway.

Use Discord interactions over Gateway for slash commands and buttons. Do not require a
public Discord interactions webhook in this cutover.

Keep Message Content intent only for owner-scoped free-form chat in DMs and configured
channels.

### Keep the droplet private by default

The droplet has a public IP, but Ariel binds to loopback only.

Allowed public inbound traffic:

- SSH from approved source addresses.
- Optional HTTPS path for Google OAuth callback only.

Everything else stays on loopback or the private admin path.

Tailscale is optional. If enabled, it is an admin plane, not a dependency for Discord.

## Target Behavior

### Chat

The owner can send a Discord DM or configured-channel message. Ariel responds with a
concise answer. If the answer needs a tool, the model calls a real capability.

Read tools can execute inline when policy allows them. External factual claims include
provenance or uncertainty.

### Approval

When a tool requires approval, Discord receives a message with:

- Action title.
- Capability id.
- Human-readable payload summary.
- Expiry time.
- Approve button.
- Deny button.

Clicking a button sends one approval decision to Ariel. The exact approved payload hash
must match the pending action. Duplicate clicks are idempotent.

Text `approve apr_xxx` is removed after the button path is live.

### Agency

The owner can ask Ariel to start code work in an allowed repo. Ariel creates an Ariel job,
calls Agency, stores the Agency identifiers, and opens a Discord job thread.

Agency progress updates appear in the thread. Status reads are available from Discord.

Remote-impacting Agency actions require approval before Ariel requests them.

### Jobs

Jobs survive process restarts.

Job status is visible in Discord and through the existing job API. Worker restarts do not
duplicate external side effects.

### Memory

Memory remains explicit, reviewable, correctable, and deletable. Tool outputs and web
content do not become trusted instructions.

### Deployment

The production droplet runs:

- `ariel-api`
- `ariel-worker`
- `ariel-discord`
- `agency-daemon`
- Managed Postgres

All services restart under systemd. Logs are structured. Backups are documented and
restore-tested.

## Capabilities

### `cap.agency.run`

Impact: `write_reversible`.

Policy: approval required.

Input:

- `repo_root`
- `task_name`
- `prompt`
- `base_branch`
- `runner`

Rules:

- `repo_root` must be in `ARIEL_AGENCY_ALLOWED_REPO_ROOTS`.
- `task_name` is a short stable label for the Agency task.
- `prompt` is the user-visible work request sent to Agency.
- `base_branch` defaults to `ARIEL_AGENCY_DEFAULT_BASE_BRANCH`.
- `runner` defaults to `ARIEL_AGENCY_DEFAULT_RUNNER`.
- `client_request_id` is the Ariel action attempt id.

Output:

- Ariel job id.
- Agency task id.
- Agency invocation id.
- Worktree path.
- Initial state.

### `cap.agency.status`

Impact: `read`.

Policy: allow inline.

Input:

- Ariel job id or Agency task id.

Output:

- Current job state.
- Latest Agency state.
- Latest summary.
- Next required user action, if any.

### `cap.agency.artifacts`

Impact: `read`.

Policy: allow inline.

Input:

- Ariel job id or Agency task id.

Output:

- Artifact list.
- Latest diff summary when available.
- Links or artifact ids for Discord rendering.

### `cap.agency.request_pr`

Impact: `external_send`.

Policy: approval required.

Input:

- Ariel job id or Agency task id.
- PR title.
- PR body.

Rules:

- The related repo must still be allowlisted.
- The pending approval payload includes the exact title, body, branch, and remote target.
- Approval executes once.

Output:

- PR URL.
- Remote branch.
- Agency state after sync.

## Discord UX

### Slash commands

Add only these commands:

- `/ask`
- `/status`
- `/jobs`
- `/memory`
- `/capture`

Do not create slash commands for every internal API.

### Buttons

Buttons are required for:

- Approve action.
- Deny action.
- Refresh job status.
- Acknowledge notification.

Each button `custom_id` contains only an opaque reference. Ariel loads the canonical
record from Postgres before deciding anything.

### Threads

Each Agency job gets one Discord thread. The thread receives:

- Start message.
- Progress summaries.
- Approval requests.
- Completion or failure message.

Do not stream every low-level runner event to Discord.

### Message content

Message Content intent remains owner-scoped. The bot ignores:

- Other users.
- Bots.
- Non-default messages.
- Messages outside configured DM/channel/mention/reply rules.

## Evals

Evals are pytest tests, not a new framework.

Use direct test cases with explicit prompts, fixtures, and assertions. Avoid an eval DSL.

Required eval groups:

- Responses function-call parsing.
- Tool routing.
- Read capability grounding and provenance.
- Approval-required action blocking.
- Approval payload hash matching.
- Prompt-injection resistance for web/email/Drive content.
- Agency run/status/artifact/PR flows.
- Discord button idempotency.
- Worker restart and stale-task recovery.
- Memory remember/correct/forget behavior.

Every eval states:

- Prompt or input event.
- Expected tool calls or no tool call.
- Expected policy decision.
- Expected user-visible response requirement.

`make verify` runs the eval suite.

## Files

### Update

- `src/ariel/app.py`
  - Delete Chat Completions code.
  - Implement the direct Responses turn loop.
  - Parse Responses function-call items at the model boundary.

- `src/ariel/action_runtime.py`
  - Replace action-proposal ingestion with Responses function-call ingestion.
  - Keep existing action attempts, approvals, payload hashes, and execution rules.

- `src/ariel/capability_registry.py`
  - Add `cap.agency.run`.
  - Add `cap.agency.status`.
  - Add `cap.agency.artifacts`.
  - Add `cap.agency.request_pr`.

- `src/ariel/config.py`
  - Remove legacy model provider settings.
  - Add Responses and Agency settings.
  - Keep bind-host loopback enforcement.

- `.env.example`
  - Match `config.py` exactly.
  - Mark required production secrets.

- `src/ariel/discord_bot.py`
  - Add slash commands.
  - Add approval buttons.
  - Add job status buttons.
  - Add Agency job threads.
  - Remove text approval command support after buttons pass tests.

- `src/ariel/worker.py`
  - Keep durable task claiming.
  - Add only the Agency/Discord delivery work needed for the new UX.
  - Keep side effects idempotent.

- `src/ariel/persistence.py`
  - Add only schema fields needed to store Discord interaction/thread ids and Agency
    identifiers.

- `alembic/versions/`
  - Add reversible migrations for new persisted fields.

- `tests/`
  - Replace old model fake tests with Responses-item tests.
  - Add Agency, Discord button, and eval coverage.

- `Makefile`
  - Keep `make verify` as the merge gate.
  - Add no new command unless `make verify` becomes unreadable.

- `README.md`
  - Replace old setup notes with the new production path.
  - Create the missing private deployment runbook.
  - Remove stale setup references.

### Delete

- Chat Completions adapter code.
- `action_proposals` production path.
- Legacy text approval command path.
- Model provider fallback code.
- Unused environment variables.
- Tests that only prove deleted behavior.

## Implementation Order

### 1. Responses hard cutover

Acceptance:

- A live OpenAI turn can call one read capability.
- The same turn can return final assistant text after the tool result.
- No Chat Completions code remains.
- No production `action_proposals` path remains.
- `make verify` passes.

### 2. Approval path on Responses function calls

Acceptance:

- Approval-required function calls create approval records.
- The model receives a function output saying approval is pending.
- Approved actions execute only the exact payload hash.
- Denied or expired actions never execute.
- Duplicate decisions are idempotent.

### 3. Agency capabilities

Acceptance:

- `cap.agency.run` starts an Agency task through the daemon socket.
- Ariel stores the Agency task and invocation ids.
- `cap.agency.status` reads current state.
- `cap.agency.artifacts` returns inspectable artifacts.
- `cap.agency.request_pr` is approval-gated.
- No shell command is used to control Agency.

### 4. Discord interaction UX

Acceptance:

- Approval buttons work.
- Job status refresh works.
- Agency job threads are created and updated.
- Slash commands cover status, jobs, memory, capture, and ask.
- Text approval commands are gone.

### 5. Evals and prompt-injection tests

Acceptance:

- Required eval groups exist as pytest tests.
- `make verify` runs them.
- At least one negative test proves untrusted web/email/Drive content cannot authorize
  tools or memory.

### 6. Production droplet runbook

Acceptance:

- systemd units are documented.
- Firewall rules are documented.
- Secret placement is documented.
- Backup and restore steps are documented.
- Health checks and log inspection are documented.
- Restart test proves API, worker, Discord, Agency, and Postgres recover.

## Final State

Ariel is a private, Discord-primary assistant with one model path, one tool-call shape, one
approval path, and one Agency control path.

The maintainer can understand a turn by reading one linear flow in `app.py`, then the
capability execution code in `action_runtime.py`.

The system is allowed to be narrow. It is not allowed to be ambiguous.
