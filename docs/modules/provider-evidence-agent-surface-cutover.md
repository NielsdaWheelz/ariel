# Provider Evidence Agent Surface Hard Cutover

## Role

This document is the implementation contract for fixing provider evidence
visibility across Ariel's agent loop, action runtime, proactive wakes, audit
events, and production posture.

The failure class is: Ariel successfully retrieves and persists provider
evidence, then returns a redacted result to the model that needs the evidence to
answer correctly. The model sees a truncated provider-sync preview, cannot see
the full bounded evidence block, and produces a user-facing answer from
incomplete context.

This is a capability/result-surface contract defect. It is not a prompt-tuning
problem.

## Authority

This document owns the cutover from one ambiguous capability output surface to
separate, typed result surfaces:

- model-visible capability output
- audit/event/log output
- durable provider evidence
- public transport output

Surrounding docs still own their narrower domains:

- [gmail-evidence-schema.md](gmail-evidence-schema.md): Gmail message evidence
  payload shape and body block contract.
- [google-workspace-reasoning-cutover.md](google-workspace-reasoning-cutover.md):
  Google Workspace product and work-graph architecture.
- [google-workspace-reasoning-completion-plan.md](google-workspace-reasoning-completion-plan.md):
  remaining Google Workspace reasoning remediation work.
- [proactivity.md](proactivity.md): provider-sync wake ingress and proactive
  loop behavior.
- [agent-loop.md](agent-loop.md): long agent loop and run-tool execution.
- [../conversational-continuity-cutover.md](../conversational-continuity-cutover.md):
  recent events window and canonical-ID recovery.
- [../production-runbook.md](../production-runbook.md): service deployment,
  health, logs, and recovery.
- [../boundaries.md](../boundaries.md), [../correctness.md](../correctness.md),
  [../cleanliness.md](../cleanliness.md), and [../ai-first.md](../ai-first.md):
  repo-wide boundary, invariant, deletion, and judgment rules.

If another doc implies that an agent-facing `cap.email.read` result may omit
body block text on `read_outcome.status == "ok"`, this document wins. If another
doc implies that audit/events/logs may store raw provider body text, that doc is
stale and must be updated or deleted during the cutover.

## Cutover Policy

- This is a hard cutover.
- No legacy code paths.
- No compatibility adapters for old redacted model-visible provider outputs.
- No fallback from a truncated provider-sync preview to user-facing summary.
- No feature flag that keeps the old one-surface behavior reachable.
- No prompt-only fix.
- No wider Gmail preview budgets as a substitute for evidence reads.
- No silent fallback to Gmail snippets, provider metadata, memory snippets, or
  previous assistant messages for body-level claims.
- Branch-local sequencing is allowed, but the merged final state contains only
  the new result-surface contract.

## Problem Statement

A provider-sync wake carries intentionally bounded changed-item evidence. For
Gmail, the wake can include a short excerpt from the normalized body evidence so
the model can decide whether to stay silent or inspect the source. That preview
is not the source of truth.

The source of truth for Gmail body content is the typed evidence returned by
`cap.email.read`, normalized by `src/ariel/google_connector.py`, persisted by
provider evidence lifecycle code, and represented as bounded blocks under
`evidence.blocks`.

Current behavior violates that architecture:

1. `cap.email.read` fetches full Gmail payloads with `format=full`.
2. Gmail normalization produces bounded text blocks.
3. Action runtime persists those blocks into `provider_evidence_blocks`.
4. Action runtime redacts `evidence.blocks[].text` from the same object that is
   returned to the model.
5. Run runtime gives the redacted object to the agent program.
6. The model cannot read the block text it asked for and may answer from the
   provider-sync preview.

The defect is therefore between the raw provider result, the durable provider
evidence store, and the model-visible capability result. Privacy redaction was
applied at the wrong boundary.

## Target Behavior

### Capability Reads

When `cap.email.read` succeeds with `read_outcome.status == "ok"`:

- Gmail has been fetched from the provider as a full message or thread.
- The provider result has passed the typed Gmail evidence validator.
- Provider evidence rows and evidence block rows have been created or reused.
- The model-visible capability result includes bounded
  `evidence.blocks[].text`.
- Every body block in the model-visible result has stable citation material:
  `block_id`, `digest`, `truncated`, and corresponding provider evidence refs.
- The audit/event/log result omits body block text and keeps only digest,
  block identity, char count, truncation state, and provider evidence refs.
- The persisted provider evidence contains the normalized block text under the
  provider evidence boundary.

When `cap.email.read` returns a non-OK read outcome:

- The model-visible capability result carries the typed unavailability outcome.
- `evidence.blocks` is empty.
- No body-derived claim may be made from that read.
- The final answer either asks for narrower/alternate evidence, reports the
  typed unavailability, or stays silent for proactive wakes.

### Provider-Sync Wakes

Provider-sync wake excerpts are previews only.

Every Gmail wake item with bounded evidence must carry explicit structure:

- `preview_kind`: `"provider_sync_preview"`
- `preview_truncated`: boolean
- `requires_read_for_body_claims`: boolean
- `message_id` or `thread_id` suitable for `email.read`
- `provider_evidence_refs` when evidence was persisted before the wake
- optional `evidence_blocks` preview text capped by the wake budget

The rendered wake text may include excerpts for model triage, but the structured
payload owns the contract. The string suffix `" [truncated]"` is not the
contract and must not be the only truncation signal.

If a Gmail provider-sync item has `preview_truncated == true`, a non-silent
assistant response that makes body-level claims about that item must be grounded
in a successful evidence read from the same message, thread, or provider
evidence refs.

### User-Facing Answers

A final assistant message may mention provider body facts only when one of these
is true:

- The current turn has a successful `cap.email.read` or
  `cap.provider_evidence.read` result with model-visible text blocks for the
  referenced source.
- The user provided the provider content directly in the current user message.
- The answer explicitly says it is based only on a preview and does not present
  body facts as complete.

For proactive provider-sync wakes, the preferred behavior when evidence is
insufficient is silence. A proactive notification from a truncated preview is
allowed only if it says the context is incomplete and avoids body-level
summaries.

### Events, Logs, and Audit

Events, action attempts, logs, API surfaces, and recent-events windows must not
store or replay private provider body text as ordinary action output. They store
redacted or compact audit output plus provider evidence refs.

Recent-events continuity composes through canonical IDs and refs:

- `message_id`
- `thread_id`
- `provider_evidence_id`
- `provider_evidence_block` refs
- action attempt IDs

The agent rehydrates evidence through capabilities, not by scraping private text
from event logs.

### Production Posture

Production service shape must match the runbook or the runbook must be changed
to match the intentional service shape. The final system exposes enough health
metadata to prove which code and config are running:

- git SHA
- service working directory
- service user
- environment source
- database migration revision
- prompt version
- capability contract hash version
- Google sync health
- provider evidence read/write health

The `/v1/events` route must satisfy its response contract. Contract validation
errors in observability routes are production bugs, not acceptable drift.

## Architecture

### Current Ownership to Reuse

The cutover reuses existing ownership instead of creating a generic framework:

- `src/ariel/google_connector.py` owns Google API calls, provider payload
  normalization, Gmail evidence constructors, and typed Google output
  validators.
- `src/ariel/google_workspace_normalization.py` owns Gmail MIME and HTML body
  normalization, text block bounding, digesting, truncation, and HTML security
  notes.
- `src/ariel/provider_evidence_lifecycle.py` owns provider evidence rows,
  evidence block rows, lifecycle transitions, restoration, supersession, and
  refs.
- `src/ariel/action_runtime.py` owns capability execution, policy, persistence,
  action attempts, events, provider evidence persistence, and result delivery to
  the model loop.
- `src/ariel/run_runtime.py` owns conversion from host-side function-call output
  to agent-program values.
- `src/ariel/sync_runtime.py` owns Google history sync, changed-item hydration,
  provider evidence production, and provider-sync wake payload construction.
- `src/ariel/worker.py` owns rendering provider-sync wakes into tainted agent
  context.
- `src/ariel/response_contracts.py` owns typed external response/event
  validation.
- `src/ariel/conversational_continuity.py` owns recent-events compaction and
  cross-turn canonical-ID resurfacing.
- `src/ariel/production_posture.py`, `scripts/verify_production_posture.py`,
  `deploy/systemd/*`, and `docs/production-runbook.md` own deploy posture.

### New Central Owner

Add one narrow module:

```text
src/ariel/provider_evidence_surface.py
```

This module owns boundary-specific projections of provider evidence-bearing
capability results. It is not a provider client, policy engine, persistence
owner, or generic sanitizer.

Public functions:

```python
def provider_capability_output_for_agent(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]: ...

def provider_capability_output_for_audit(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]: ...

def provider_capability_output_for_public_transport(
    *,
    capability_id: str,
    output_payload: dict[str, Any],
) -> dict[str, Any]: ...
```

Rules:

- `*_for_agent` preserves bounded evidence block text that the capability
  contract says the model may inspect.
- `*_for_audit` removes private provider body text and private calendar
  descriptions, replacing them with digest, char count, block identity, and
  redaction markers.
- `*_for_public_transport` is at least as restrictive as audit output unless a
  route has a separate explicit user-authorized content contract.
- The functions defect on unknown Google provider output shapes that contain
  private body-bearing fields.
- The functions do not fetch provider data, mutate the database, decide
  semantic importance, or inspect prompt text.

Move the existing Google output redaction helpers from `action_runtime.py` into
this module and make call sites name the target surface explicitly. A generic
`_redact_google_provider_output` helper is deleted.

## Result Surface Contract

### Raw Provider Result

Raw provider result is the connector output returned by `execute_capability`.
It is trusted only after the capability-specific typed validator accepts it.

Allowed lifetime:

- local variable inside action execution
- input to provider evidence persistence
- input to result-surface projection

Forbidden:

- storing raw provider body text in action attempts
- putting raw provider body text in events
- returning raw provider body text to public API routes
- rendering raw provider body text into logs

### Durable Provider Evidence

Durable provider evidence is the canonical private content store.

Existing tables remain the source of truth:

- `provider_evidence`
- `provider_evidence_blocks`

Required invariants:

- `provider_evidence.lifecycle_state == "available"` is required for
  dereference.
- `provider_evidence.sensitivity` controls non-model surfaces.
- `provider_evidence.taint == "provider_untrusted"` reaches model-visible
  evidence provenance.
- `provider_evidence_blocks.text` is never used as an event/log transport.
- Provider evidence refs include enough block IDs to cite or dereference the
  exact bounded blocks used in a final answer.

### Model-Visible Capability Output

Model-visible output is the object returned to the run program through
`ctx.function_call_outputs` and `_capability_syscall_value`.

For `cap.email.read` with `read_outcome.status == "ok"`, the output must retain
the Gmail evidence schema and include:

```json
{
  "schema_version": "google.gmail.message_evidence.v1",
  "status": "succeeded",
  "mode": "message",
  "message": {
    "message_id": "msg_...",
    "thread_id": "thr_...",
    "subject": "...",
    "sender": "...",
    "body": {
      "preferred_mime_type": "text/plain",
      "truncated": false,
      "body_digest": "..."
    }
  },
  "evidence": {
    "source_kind": "gmail_message",
    "message_id": "msg_...",
    "thread_id": "thr_...",
    "body_digest": "...",
    "truncated": false,
    "blocks": [
      {
        "block_id": "gmail:msg_...:body:0:...",
        "kind": "body",
        "text": "bounded body evidence...",
        "digest": "...",
        "truncated": false,
        "source_mime_type": "text/html",
        "charset": "utf-8"
      }
    ]
  },
  "read_outcome": {
    "status": "ok",
    "reason_code": null,
    "recovery": null
  },
  "provider_evidence_refs": [
    {
      "provider_evidence_id": "pev_...",
      "read_receipt_id": "pev_...",
      "source_kind": "gmail_message",
      "external_id": "msg_...",
      "thread_external_id": "thr_...",
      "block_ids": ["peb_..."],
      "citation_refs": [
        {"kind": "provider_evidence_block", "block_id": "peb_..."}
      ]
    }
  ],
  "runtime_provenance": {
    "status": "tainted",
    "evidence": [
      {
        "kind": "provider_evidence",
        "provider_evidence_id": "pev_...",
        "taint": "provider_untrusted",
        "sensitivity": "private"
      }
    ]
  }
}
```

`message.body` may contain metadata about the body. It must not contain body
text. Body text lives only under `evidence.blocks`.

### Audit/Event Output

Audit output is the object stored in `ActionAttemptRecord.execution_output`,
emitted in `evt.action.execution.succeeded`, exposed through `/v1/events`, and
rendered into the recent-events window.

For the same `cap.email.read`, audit output must include:

```json
{
  "schema_version": "google.gmail.message_evidence.v1",
  "status": "succeeded",
  "mode": "message",
  "message": {
    "message_id": "msg_...",
    "thread_id": "thr_...",
    "subject": "...",
    "sender": "...",
    "body": {
      "preferred_mime_type": "text/plain",
      "truncated": false,
      "body_digest": "..."
    }
  },
  "evidence": {
    "source_kind": "gmail_message",
    "message_id": "msg_...",
    "thread_id": "thr_...",
    "body_digest": "...",
    "truncated": false,
    "blocks": [
      {
        "block_id": "gmail:msg_...:body:0:...",
        "kind": "body",
        "digest": "...",
        "truncated": false,
        "text_redacted": true,
        "text_digest": "...",
        "text_char_count": 1957,
        "source_mime_type": "text/html",
        "charset": "utf-8"
      }
    ]
  },
  "read_outcome": {
    "status": "ok",
    "reason_code": null,
    "recovery": null
  },
  "provider_evidence_refs": [
    {
      "provider_evidence_id": "pev_...",
      "read_receipt_id": "pev_...",
      "source_kind": "gmail_message",
      "external_id": "msg_...",
      "thread_external_id": "thr_...",
      "block_ids": ["peb_..."],
      "citation_refs": [
        {"kind": "provider_evidence_block", "block_id": "peb_..."}
      ]
    }
  ]
}
```

Audit output never contains `evidence.blocks[].text`.

## Capability Contract and API Design

### `cap.email.read`

Existing capability. Contract changes:

- The connector output contract remains `google.gmail.message_evidence.v1`.
- The model-visible result must satisfy the Gmail evidence schema and preserve
  block text for `ok` reads.
- The audit/event result must satisfy an audit projection contract that forbids
  block text.
- The action runtime defects if it cannot create provider evidence refs for an
  `ok` read.
- The action runtime defects if the model-visible result for an `ok` read lacks
  block text.
- The action runtime defects if the audit result for any provider read contains
  private block text.

No caller is allowed to treat `text_redacted == true` as successful model-visible
body evidence.

### `cap.provider_evidence.read`

Add one new model-facing capability for dereferencing already-authorized
provider evidence rows.

Run callable name:

```text
provider_evidence.read
```

Input:

```json
{
  "provider_evidence_id": "pev_...",
  "block_ids": ["peb_..."],
  "max_blocks": 12
}
```

Rules:

- `provider_evidence_id` is required.
- `block_ids` is optional. If present, every block must belong to the evidence
  row.
- `max_blocks` is optional and capped by the same provider evidence block budget
  used for Gmail read model output.
- Only `lifecycle_state == "available"` rows can be read.
- `redacted`, `deleted`, `superseded`, `stale`, and `unavailable` rows return a
  typed non-OK result. They do not silently read another row.
- The capability does not call Google. It reads Ariel's provider evidence store.
- The capability returns text only through model-visible output. Audit/event
  output redacts block text.

Model-visible output:

```json
{
  "schema_version": "provider.evidence_blocks.v1",
  "status": "succeeded",
  "read_outcome": {
    "status": "ok",
    "reason_code": null,
    "recovery": null
  },
  "provider_evidence": {
    "provider_evidence_id": "pev_...",
    "provider": "google",
    "source_kind": "gmail_message",
    "external_id": "msg_...",
    "thread_external_id": "thr_...",
    "content_digest": "...",
    "taint": "provider_untrusted",
    "sensitivity": "private",
    "lifecycle_state": "available",
    "observed_at": "2026-05-27T00:48:30Z"
  },
  "blocks": [
    {
      "block_id": "peb_...",
      "block_index": 0,
      "kind": "body",
      "text": "bounded evidence block...",
      "digest": "...",
      "truncated": false,
      "source_offsets": {"block_id": "gmail:msg_...:body:0:..."}
    }
  ],
  "runtime_provenance": {
    "status": "tainted",
    "evidence": [
      {
        "kind": "provider_evidence",
        "provider_evidence_id": "pev_...",
        "taint": "provider_untrusted",
        "sensitivity": "private"
      }
    ]
  }
}
```

Non-OK output:

```json
{
  "schema_version": "provider.evidence_blocks.v1",
  "status": "succeeded",
  "read_outcome": {
    "status": "unavailable",
    "reason_code": "provider_evidence_lifecycle_state",
    "recovery": "Read the current provider source by canonical provider ID."
  },
  "provider_evidence": {
    "provider_evidence_id": "pev_...",
    "lifecycle_state": "superseded"
  },
  "blocks": []
}
```

This capability is for dereference, not fallback. If the row is stale or
superseded, the model must use `email.read` or the relevant provider capability
with a canonical provider ID.

### Provider-Sync Grounding Gate

Add one deterministic grounding gate for provider-sync wakes.

Inputs:

- wake provenance from `WakeContext`
- structured provider-sync payload from `sync_runtime.py`
- action attempts and model-visible capability results from the current turn
- proposed final assistant message or silent outcome

Rules:

- Silent outcome always passes.
- Non-silent Gmail provider-sync outcome passes when no wake item has
  `requires_read_for_body_claims == true`.
- Non-silent Gmail provider-sync outcome passes when the current turn has a
  successful `cap.email.read` or `cap.provider_evidence.read` result for every
  item whose body facts are used.
- If exact per-item usage cannot be proven from the final text, the gate uses a
  conservative wake-level rule: any non-silent response to a wake with truncated
  Gmail previews requires at least one successful evidence read from the wake's
  message/thread/evidence refs.
- Failure does not produce a user-visible answer. The loop receives a system
  nudge: read the specific source or stay silent.
- Repeated failure ends the turn as silent with a typed internal error event.

This gate is a rail, not semantic ranking. It does not decide whether an email is
important. It enforces that body-level output is grounded in body evidence.

## Files and Required Changes

### Documentation

- `docs/modules/provider-evidence-agent-surface-cutover.md`
  - New owner document.
- `docs/modules/index.md`
  - Link this document.
- `docs/modules/gmail-evidence-schema.md`
  - Clarify that the Gmail evidence schema describes connector and
    model-visible evidence, while audit projections must redact block text.
- `docs/modules/proactivity.md`
  - Clarify provider-sync preview structure and `requires_read_for_body_claims`.
- `docs/conversational-continuity-cutover.md`
  - Clarify that recent events carry audit output plus refs, and rehydration uses
    `email.read` or `provider_evidence.read`.
- `docs/production-runbook.md`
  - Align service layout with reality or update services to match the runbook.

### Runtime

- `src/ariel/provider_evidence_surface.py`
  - New central owner for agent/audit/public provider output projections.
- `src/ariel/action_runtime.py`
  - Stop mutating one `execution_result.output` into the only output.
  - Keep raw connector output local.
  - Persist provider evidence from raw output.
  - Build `agent_output` and `audit_output` explicitly.
  - Send `agent_output` through `ctx.function_call_outputs`.
  - Store `audit_output` in action attempts and events.
  - Use explicit provider surface helper names at every boundary.
- `src/ariel/run_runtime.py`
  - Continue returning nested `payload["output"]`, now guaranteed to be
    model-visible output for run-tool calls.
  - Add tests proving `email.read(...).evidence.blocks[0].text` is visible to
    agent code on `ok` reads.
- `src/ariel/capability_registry.py`
  - Add `cap.provider_evidence.read` and run callable signature.
  - Tighten `cap.email.read` contract metadata to state model-visible block text
    is required for `ok`.
- `src/ariel/google_connector.py`
  - Keep owning Gmail evidence construction and validation.
  - Add or expose validators needed by surface tests if current validators are
    connector-private and duplicated elsewhere.
- `src/ariel/response_contracts.py`
  - Add audit projection validators if events/API routes need strict validation
    for redacted provider evidence output.
  - Fix `/v1/events` contract handling rather than weakening validation.
- `src/ariel/sync_runtime.py`
  - Add structured preview fields.
  - Carry provider evidence refs in wake items when available.
  - Stop relying on text suffix truncation as the contract.
- `src/ariel/worker.py`
  - Render provider-sync previews with explicit "preview" wording.
  - Keep structured tainted provenance for wake gates.
- `src/ariel/agent_loop.py` or the closest finalization owner
  - Add provider-sync grounding gate at final-message boundary.
- `src/ariel/prompts.py`
  - Update wording only after runtime contracts exist. Prompt text is not the
    enforcement mechanism.

### Persistence and Migrations

No schema migration is required for the minimal cutover if
`ActionAttemptRecord.execution_output` is defined as audit output.

If implementation shows that column semantics are too ambiguous, perform a hard
schema rename instead of adding aliases:

- `action_attempts.execution_output` -> `execution_audit_output`
- update all references
- no compatibility property
- no dual-write period

Do not add a second durable copy of provider body text outside
`provider_evidence_blocks`.

### Production and Operations

- `deploy/systemd/*.service`
  - Match the production runbook or intentionally update the runbook.
- `scripts/verify_production_posture.py`
  - Verify service working directory, service user, env source, git SHA exposure,
    migration revision, and provider evidence surface health.
- `src/ariel/app.py`
  - `/v1/health` includes version/config/migration evidence.
  - `/v1/events` satisfies response contracts with audit-safe output.
- `docs/manual-smoke-test.md`
  - Add a live controlled Gmail read smoke that confirms body evidence is
    model-visible without printing private message text in the docs.

## Key Decisions

### Separate Surfaces Instead of Redaction Toggle

Redaction is correct for events, logs, and public transports. It is wrong for
the model-visible result of an authorized read capability. The fix is separate
surfaces, not disabling redaction.

### Provider Evidence Remains Canonical

The model-visible output is a bounded current-turn view. The durable source of
truth remains `provider_evidence_blocks`. The agent can reread provider evidence
through a capability when recent events only contain refs.

### Wakes Stay Small

Provider-sync wakes are triage inputs. They should not grow until they are full
email reads in disguise. The wake budget remains bounded; the agent reads the
specific source when it needs completeness.

### Runtime Gate Over Prompt Instruction

The prompt can say "read if truncated", but the system must enforce the invariant
that a non-silent body-level proactive answer has evidence. This belongs in
runtime finalization because the defect is safety/correctness, not style.

### Hard Cutover Over Mixed Shapes

There is no supported old shape where `cap.email.read` returns `text_redacted`
instead of body block text to the model. Tests must reject that shape in the
model-visible path.

### No Generic Evidence Framework

This cutover creates a narrow provider evidence surface owner because there is a
real boundary defect. It does not introduce a generic document store, workflow
engine, or provider-independent abstraction.

## Acceptance Criteria

### Capability Output

- A unit test proves `provider_capability_output_for_agent` preserves
  `evidence.blocks[].text` for `cap.email.read` `ok` results.
- A unit test proves `provider_capability_output_for_audit` removes
  `evidence.blocks[].text`, adds digest/char count metadata, and preserves
  provider evidence refs.
- A unit test proves unknown private body-bearing Google output shapes defect
  instead of passing through.
- An integration test proves a run program can call
  `email.read(message_id=..., mode="message")` and read the full bounded block
  text from the returned value.
- An integration test proves `ActionAttemptRecord.execution_output` and
  `evt.action.execution.succeeded.payload.output` contain audit-safe redacted
  output for the same read.

### Gmail Evidence

- `cap.email.read` `ok` model-visible output without non-empty block text fails.
- `cap.email.read` non-OK output with body blocks fails.
- `cap.email.read` `ok` output without provider evidence refs fails.
- Gmail thread mode has the same model-visible/audit split as message mode.
- Gmail search remains refs and preview metadata only. It never becomes body
  evidence.

### Provider Evidence Dereference

- `provider_evidence.read` returns bounded text blocks for available evidence.
- `provider_evidence.read` rejects block IDs that do not belong to the evidence
  row.
- `provider_evidence.read` returns typed non-OK outcomes for redacted, deleted,
  superseded, stale, and unavailable rows.
- Audit/event output for `provider_evidence.read` redacts block text.
- Recent-events output plus provider evidence refs is sufficient for the agent
  to rehydrate text through `provider_evidence.read`.

### Provider-Sync Wakes

- Gmail wake payloads include `preview_truncated` and
  `requires_read_for_body_claims`.
- Wake rendering labels body excerpts as preview excerpts.
- A provider-sync wake with a truncated Gmail preview and no evidence read cannot
  produce a non-silent body summary.
- The same wake can produce a correct non-silent summary after `email.read`
  returns model-visible block text.
- Silent outcomes remain valid for routine provider-sync wakes.

### Regression Scenario

Use a fixture shaped like the documented failure:

- The provider-sync wake excerpt includes only the first part of a schedule and
  `preview_truncated == true`.
- The full Gmail read contains additional schedule rows after the preview.
- The model-visible `email.read` result includes the full bounded block text.
- The final assistant message includes facts from beyond the wake preview.
- The final assistant message does not say the source message was truncated when
  `evidence.truncated == false`.
- The audit event for the read does not contain private body text.

### Operations

- `/v1/health` reports enough version/config posture to connect logs to code.
- `/v1/events` passes response contract validation.
- Production posture verification detects service-user, working-directory, env
  source, and git-SHA drift.
- Google sync repeated typed-output failures are visible in health or posture
  checks.
- Manual smoke docs include commands for proving that model-visible read output
  exists without dumping private email content into documentation.

## Non-Goals

- No larger Gmail body budgets.
- No provider-sync wake as a full email transport.
- No model memory as evidence source for private provider body claims.
- No public API for arbitrary provider body dump.
- No raw Gmail payload storage.
- No autonomous email/calendar write behavior changes.
- No non-Google provider work.
- No new semantic ranking or importance classifier.
- No general archive/search product.
- No compatibility for old redacted model-visible `email.read` outputs.

## Implementation Plan

### Phase 1 - Failing Tests and Contracts

Add tests that capture the desired final behavior before implementation:

1. `tests/unit/test_provider_evidence_surface.py`
   - agent surface keeps bounded text
   - audit surface redacts bounded text
   - private unknown shapes defect
2. `tests/integration/test_google_connector_read_acceptance.py`
   - run-visible `email.read` has block text
   - stored/event output redacts block text
3. `tests/integration/test_sync_runtime_provider_ingestion.py`
   - wake payload has structured preview fields and refs
4. New provider-sync finalization test in the closest existing proactivity or
   agent-loop suite
   - truncated preview without read cannot produce body summary
   - truncated preview with read can produce body summary
5. `tests/unit/test_responses_tool_contract.py`
   - `provider_evidence.read` signature and contract

### Phase 2 - Centralize Result Surfaces

Create `provider_evidence_surface.py`.

Move provider output redaction helpers out of `action_runtime.py`. Replace the
single mutation of `execution_result.output` with explicit variables:

```python
raw_output = execution_result.output
provider_evidence_refs = persist_from(raw_output)
raw_output_with_refs = attach_refs(raw_output, provider_evidence_refs)
agent_output = provider_capability_output_for_agent(...)
audit_output = provider_capability_output_for_audit(...)
```

Use:

- `agent_output` for `ctx.function_call_outputs`
- `agent_output` for current-turn interpreter/model-visible inline results
- `audit_output` for `ActionAttemptRecord.execution_output`
- `audit_output` for events
- `audit_output` for public API/event surfaces

Delete ambiguous helper names and old call sites.

### Phase 3 - Add Provider Evidence Dereference

Add `cap.provider_evidence.read` through the existing capability registry and
action runtime path.

Keep the implementation DB-backed and narrow:

- query `ProviderEvidenceRecord`
- enforce lifecycle
- query ordered `ProviderEvidenceBlockRecord` rows
- bound block count and text length
- return model/audit split through the same surface owner

Do not call Google from this capability.

### Phase 4 - Provider-Sync Preview Contract

Update sync wake payload construction:

- structured preview fields
- provider evidence refs
- explicit read-required boolean
- no reliance on textual truncation suffix

Update worker rendering to present excerpts as previews and preserve structured
tainted provenance.

### Phase 5 - Grounding Gate

Add finalization enforcement for provider-sync wakes.

The first implementation can use the conservative wake-level rule. A later
change may narrow to per-item proof only if the final-message surface becomes
structured enough to support it without model judgment in deterministic code.

### Phase 6 - Production Posture

Fix `/v1/events` contract errors and align deployment docs/services.

Expose health/posture metadata and add posture checks so a future investigation
does not have to infer which service shape is live from systemd and journals.

### Phase 7 - Documentation and Smoke

Update related docs and manual smoke tests. Remove stale statements that imply
redacted model-visible reads are acceptable.

## Test Matrix

Required local verification:

```text
python -m pytest tests/unit/test_provider_evidence_surface.py
python -m pytest tests/unit/test_google_connector_hardening.py
python -m pytest tests/unit/test_google_typed_output_enforcement.py
python -m pytest tests/unit/test_responses_tool_contract.py
python -m pytest tests/integration/test_google_connector_read_acceptance.py
python -m pytest tests/integration/test_sync_runtime_provider_ingestion.py
python -m pytest tests/integration/test_proactivity_scheduler.py
python -m pytest tests/unit/test_response_contracts_events.py
python -m pytest tests/unit/test_production_posture.py
python -m pytest tests/unit/test_deploy_artifacts.py
```

Required contract/static verification:

```text
python -m compileall src tests
python -m pytest --collect-only
git diff --check
```

Required production verification after deploy:

```text
systemctl status ariel-api ariel-worker ariel-pubsub ariel-discord --no-pager
curl -fsS http://127.0.0.1:8000/v1/health
curl -fsS http://127.0.0.1:8000/v1/events
journalctl -u ariel-worker --since "30 min ago" --no-pager
```

The live Gmail smoke must use a controlled message or a redacted assertion. The
verification may assert that block text exists and has the expected digest or
length; it must not paste private message body text into docs, logs, or final
reports.

## Final State

After this cutover:

- The agent can read full bounded Gmail evidence through `email.read`.
- The agent can dereference persisted provider evidence by evidence refs.
- Provider-sync previews remain small and explicitly marked as previews.
- A truncated provider-sync preview cannot produce an unsupported body summary.
- Events, logs, action attempts, recent-events windows, and public routes do not
  leak private provider body text.
- Provider evidence refs compose across current-turn tools, recent-events
  continuity, memory/retrieval, proactive wakes, provider writes, and audit.
- Production posture makes code/config drift visible.
- The old redacted-model-output behavior is gone.
