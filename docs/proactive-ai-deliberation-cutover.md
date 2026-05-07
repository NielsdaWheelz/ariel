# Proactive AI Deliberation Cutover

## Scope

This document defines Ariel's hard cutover from deterministic proactive attention
ranking into event-triggered AI deliberation, ambient always-on sensing, and
autonomous action.

The cutover covers provider-triggered deliberation, ambient observation ingestion,
context assembly, model decision contracts, proactive turns, autonomous action
plans, policy validation, durable execution, feedback learning, inspection APIs,
operator recovery, and acceptance tests.

It does not cover scheduled prompt products, daily briefing products, generic
automation-builder UI, provider-hosted memory, model fine-tuning, mobile push, or
unsupported sensor integrations.

## Cutover Policy

- This is a hard cutover.
- Remove deterministic attention ranking as Ariel's proactive decision maker.
- Remove delivery decisions based on categorical priority, heuristic rank score,
  source-local thresholds, or deterministic delivery categories.
- Remove scheduled-prompt and daily-briefing semantics from the proactive product
  surface.
- Remove old attention task names, old attention response contracts, old attention
  item lifecycle assumptions, old notification thresholds, and old tests.
- Remove one-signal-one-item review and grouped deterministic ranking.
- Do not keep compatibility code for old `attention_*` tables, task payloads,
  endpoints, response fields, Discord action labels, or notification semantics.
- Do not dual-write old attention records and new proactive records.
- Do not add feature flags that make old and new proactive systems reachable in
  the same deployment.
- Do not add fallback paths that let deterministic code decide whether Ariel
  should speak, wait, suppress, or act.
- Do not let transport code initiate proactive work directly.
- Do not fake a user message to create an unprompted assistant turn.
- Do not let free-form model text execute external side effects. Autonomous action
  must flow through a structured decision contract and policy validation.
- No intermediate state is production-shippable while old attention ranking and
  new AI deliberation are both reachable.

## Product Thesis

Ariel should not be a scheduler, digest generator, or rule-based notification
system. Ariel should be an always-on operator that notices meaningful changes,
assembles context, decides whether intervention is warranted, speaks first when
useful, and acts without asking when the action is inside an approved autonomy
scope.

The model owns judgment:

- whether this event matters
- whether now is the right time
- what evidence is relevant
- whether more search or tool use is needed
- whether to speak, wait, remember, ask, or act
- what to say
- what exact action plan to execute

Deterministic code owns rails:

- ingestion
- parsing and validation
- idempotency
- provenance
- taint
- permissions
- autonomy scopes
- external side-effect boundaries
- persistence
- replay
- recovery
- inspection
- operator controls

Policy validation can veto or constrain a model decision. It must not replace the
model's proactive judgment with a deterministic ranking substitute.

## Goals

- Ariel deliberates on events as they arrive instead of waiting for schedules or
  summaries.
- Ariel continuously senses enabled ambient sources and turns observations into
  durable deliberation opportunities.
- Ariel speaks first when the model concludes the user should know now.
- Ariel can autonomously execute approved actions without per-action confirmation.
- Every proactive decision is inspectable: what event triggered it, what context
  was assembled, what tools were used, what the model decided, what policy allowed
  or denied, and what side effect happened.
- Proactive turns are first-class assistant-originated turns, not synthetic user
  turns.
- Feedback teaches future deliberation through durable model instructions,
  examples, scope changes, and preference records.
- Taint and provenance survive through observation, context assembly, model
  deliberation, proactive turn, action plan, execution, and feedback.
- The system remains correct across retries, duplicate events, restarts,
  concurrent workers, provider outages, stale connectors, and partial external
  side effects.
- Operators can replay, inspect, pause, resume, and dead-letter every proactive
  workflow stage.

## Target Behavior

### User Experience

- Ariel contacts the user first when timely context makes intervention useful:
  "Sir, you should leave for the airport now."
- Ariel can explain:
  - "why did you speak?"
  - "why now?"
  - "what changed?"
  - "what did you look at?"
  - "what did you ignore?"
  - "what did you do?"
  - "why were you allowed to do that?"
  - "what would have made you stay silent?"
  - "make this less aggressive"
  - "do this kind of thing automatically next time"
- Ariel can act without a prompt when an event and context justify action and the
  action is inside an approved autonomy scope.
- Ariel does not ask for confirmation for actions the user has delegated.
- Ariel asks only when the decision requires user judgment, missing authority, or
  unavailable evidence.
- Ariel stays silent when the model decides the event is noise, stale,
  already-handled, or not worth interrupting.
- Ariel never implies an action happened unless the action runtime recorded the
  exact completed side effect.
- Ariel never hides autonomous actions. Every action has an audit trail and a
  user-visible correction path.

### Event-Triggered Deliberation

- Provider events, internal state transitions, ambient observations, captures,
  memory changes, job changes, connector health changes, and external search
  results can open or update a proactive case.
- Each case triggers context assembly and model deliberation.
- The model can call read-only tools during deliberation when the context bundle
  is insufficient.
- The model chooses one explicit decision:
  - `ignore`
  - `remember`
  - `wait`
  - `observe_more`
  - `speak_now`
  - `ask_user`
  - `act_now`
  - `speak_and_act`
- `wait` must include a concrete recheck condition or time.
- `observe_more` must name the missing signal or source.
- `speak_now` and `ask_user` must include bounded user-facing copy.
- `act_now` and `speak_and_act` must include exact action plans.

### Ambient Always-On Sensing

- Ambient sensing means Ariel continuously observes enabled sources, not that it
  reads arbitrary systems without consent or configuration.
- Enabled sources produce durable observations:
  - Discord messages and interactions
  - Google Calendar changes
  - Gmail thread changes
  - Drive file changes
  - captures
  - job and approval state
  - memory commitments and preferences
  - connector health
  - configured local or browser activity
  - configured location or travel context
  - configured repository, CI, or incident streams
- Observations are normalized at ingress and carry source, timestamp, trust
  boundary, taint, actor, subject, and dedupe key.
- Ambient observers never notify or act directly.
- The durable worker queues ambient observation derivation on
  `ARIEL_PROACTIVE_AMBIENT_INTERVAL_SECONDS`; transport and inspection APIs do not own
  normal ambient sensing.
- Ambient observation compression is allowed only as a derived projection. The
  canonical observation log remains durable and replayable.
- Sensor failure opens a proactive case when the model should decide whether the
  missing sensor itself matters.

### Autonomous Action

- Free autonomous action means no per-action confirmation for action classes the
  user has granted to Ariel.
- Each granted autonomy scope defines:
  - actor
  - source context
  - action type
  - allowed target systems
  - allowed payload shape
  - max impact
  - revocation rule
  - notification rule
  - audit visibility
- The model can propose exact actions inside those scopes.
- Policy validation authorizes, denies, or narrows the exact action plan.
- Autonomous external writes require provider idempotency keys when available and
  durable local idempotency keys always.
- The action runtime records every external side effect before the proactive case
  is marked complete.
- High-impact domains are denied unless the product has explicit domain-specific
  policy and tests. This includes money movement, legal filing, medical treatment
  decisions, credential disclosure, destructive deletion, and public posting.

### Feedback

- Feedback is a first-class input to future deliberation.
- Explicit user feedback can update autonomy scopes, model instructions, case
  examples, source preferences, notification aggressiveness, and action defaults.
- Inferred feedback can adjust examples and confidence calibration. It cannot
  grant new autonomy scopes.
- Corrections supersede prior preferences instead of overwriting history.
- Feedback is inspectable and reversible.

## Architecture

The primary pipeline is:

1. Ingress writes raw provider event records or ambient source records.
2. Normalizers create canonical `proactive_observations`.
3. Case routing opens or updates a durable `proactive_case`.
4. Context assembly creates a `proactive_context_snapshot`.
5. Model deliberation creates an append-only `proactive_decision`.
6. Policy validation evaluates the structured decision and exact action plans.
7. Execution delivers proactive turns or runs authorized autonomous actions.
8. Follow-up rechecks open cases when the model chose `wait` or `observe_more`.
9. Feedback learning updates durable instructions, examples, preferences, and
   autonomy scopes.
10. Inspection APIs expose every step from observation to decision to action.

Provider event records and ambient source records are ingress hints. Proactive
observations, cases, context snapshots, decisions, policy validations, turns, and
action executions are the source of truth for the proactive system.

## Structure

### Canonical Records

- `proactive_observations`: normalized event and ambient observations with source
  references, dedupe key, actor, subject, timestamp, trust boundary, taint, and
  evidence pointers.
- `proactive_cases`: durable unit of proactive deliberation, keyed by the
  situation Ariel is deciding about.
- `proactive_case_events`: append-only lifecycle events for case creation,
  update, deliberation, validation, delivery, action, feedback, and recovery.
- `proactive_context_snapshots`: immutable context bundles shown to the model,
  including memory references, recent history references, tool outputs, search
  outputs, sensor state, omitted-context diagnostics, and taint summary.
- `proactive_decisions`: append-only structured model decisions with model id,
  prompt version, decision type, confidence, urgency, rationale, evidence refs,
  proposed message, proposed actions, and follow-up condition.
- `proactive_policy_validations`: deterministic validation results for a specific
  decision id and action plan hash.
- `proactive_turns`: assistant-originated user-visible turns with delivery state,
  channel, rendered text, linked decision id, and linked case id.
- `proactive_action_plans`: exact structured actions proposed by the model.
- `proactive_action_executions`: durable execution state and external side-effect
  receipts for authorized action plans.
- `autonomy_scopes`: user-granted or operator-granted action authority.
- `proactive_feedback`: explicit and inferred feedback events.
- `proactive_learning_records`: durable instruction, example, calibration, and
  preference changes derived from feedback.

### Derived Projections

The following are rebuildable projections:

- observation summaries
- case search indexes
- vector embeddings
- context candidate indexes
- deliberation examples
- calibration metrics
- source reliability metrics
- action success metrics
- user interruption-cost metrics

Projections can accelerate model context assembly. They cannot be the source of
truth for proactive decisions or side effects.

### Model Decision Contract

Every deliberation call returns one structured decision:

```json
{
  "decision": "speak_and_act",
  "confidence": 0.87,
  "urgency": "high",
  "user_visible_message": "Sir, you should leave for the airport now.",
  "rationale": "Calendar departure, current drive time, and preferred buffer imply leaving now.",
  "evidence_refs": ["obs_calendar_123", "tool_maps_eta_456", "mem_pref_buffer_789"],
  "tool_refs": ["tool_maps_eta_456"],
  "actions": [
    {
      "action_type": "send_discord_message",
      "target": "primary_user",
      "payload": {"text": "Sir, you should leave for the airport now."},
      "risk_tier": "low",
      "idempotency_key": "proactive-case-abc:speak:leave-airport"
    }
  ],
  "follow_up": {
    "condition": "if_not_acknowledged",
    "after": "PT10M"
  }
}
```

Rules:

- The model must cite evidence refs for every `speak_now`, `ask_user`,
  `act_now`, and `speak_and_act` decision.
- The model must include exact payloads for proposed actions.
- The model must include a confidence value, but confidence is an input to policy,
  not a deterministic rank threshold.
- The model may decide `ignore`; ignored decisions are still stored.
- The model may decide `wait`; waits are durable and rechecked.
- Invalid JSON, invalid enum values, missing evidence refs, missing action
  payloads, or schema violations fail the deliberation task. They do not trigger
  deterministic fallback behavior.

### Policy Validation

Policy validation is not the proactive brain. It answers only:

- Is the decision schema valid?
- Is the evidence accessible to this user and this model?
- Does taint allow this message or action?
- Does the selected channel allow proactive delivery?
- Is the action inside an active autonomy scope?
- Is the exact payload allowed by that scope?
- Is the operation idempotent?
- Is the target system available?
- Would this duplicate a completed turn or side effect?
- Does a hard safety block apply?

Policy validation can produce:

- `authorized`
- `authorized_with_constraints`
- `denied`
- `needs_user_authority`
- `stale_context`
- `invalid_decision`
- `duplicate`
- `dead_letter`

Policy validation must record the exact decision id, action plan hash, policy
version, result, constraints, and denial reason.

### Proactive Turns

- A proactive turn is an assistant-originated turn with no user prompt.
- It records `origin="proactive"`, `case_id`, `decision_id`, `channel`, and
  delivery state.
- It can be delivered through Discord or any future channel.
- It emits the same surface event shape expected by clients, with an explicit
  proactive origin.
- It never stores a fake user message.
- It can include controls: acknowledge, correct, stop this pattern, make this more
  aggressive, undo action when supported, and inspect why.

### Autonomous Execution

- The action runtime executes only policy-authorized action plans.
- Each action has a deterministic idempotency key and payload hash.
- External calls happen outside database transactions.
- External side-effect receipts are stored before the case advances.
- Retries resume from recorded execution state.
- Undo is exposed only when the action has a tested compensating action.
- Failed autonomous execution can itself trigger a new proactive case.

## Rules

### AI Judgment

- The model decides whether Ariel speaks, waits, asks, ignores, remembers, or acts.
- Deterministic code must not compute a replacement rank, priority threshold, or
  delivery decision.
- Deterministic code may validate, authorize, deny, dedupe, pause, retry, render,
  and recover.
- The model sees structured context plus authorized read-only tool outputs.
- The model may request more context during deliberation.
- The model cannot mutate durable state except by returning the structured
  decision contract.

### Context

- Every context snapshot is immutable and linked to a case and decision.
- Context assembly can include raw-ish provider content only after ingress parsing,
  access checks, taint labeling, and budget selection.
- Prompt-injection-bearing content remains tainted and cannot grant authority,
  modify policy, change autonomy scopes, or suppress safety checks.
- Memory context comes from Ariel's memory system, not model-provider hosted
  memory.
- Omitted relevant context is recorded as diagnostics.
- Context snapshot ids, not ad hoc prompt strings, are used for replay and audit.

### Observations

- Observers produce observations, not notifications.
- Observations have stable dedupe keys.
- Duplicate provider events do not duplicate cases, decisions, turns, or actions.
- Observation trust level is explicit.
- Ambient sensors are source-scoped and revocable.
- Missing or stale sensor data is visible to the model as uncertainty.

### Autonomy

- Autonomous action requires an active autonomy scope.
- Autonomy scopes are durable, inspectable, revocable, and versioned.
- Scope changes are not inferred from passive behavior.
- The model can ask for a new scope, but cannot grant it.
- External write actions require exact payload hashes.
- High-impact action classes remain blocked until implemented as explicit
  domain-specific policies with acceptance tests.
- The runtime must not silently downgrade `act_now` into `speak_now`; it records
  `needs_user_authority` or `denied` and then starts a new deliberation if useful.

### Delivery

- Delivery exists to render a proactive turn or action result.
- Delivery must not decide whether the turn should exist.
- Delivery is schema-deduped.
- Delivery failures do not erase the decision or action execution record.
- Discord remains a channel, not a proactive work owner.
- Mentions are denied by default unless an autonomy scope explicitly allows them.

### Learning

- Feedback creates durable learning records through a worker step.
- Learning records can alter future context, prompt instructions, examples,
  autonomy scopes, and calibration.
- Learning records cannot override hard policy blocks.
- Negative feedback must be visible in future deliberation context.
- Positive feedback can make Ariel more aggressive for matching patterns without
  adding deterministic thresholds.

## Operations

- Every mutating operation declares idempotency, transaction boundary, and replay
  behavior.
- Every worker task has an exact payload. Broad queue scans are not fallback
  behavior.
- Worker-owned ambient derivation and provider-renewal follow-up tasks use
  `ARIEL_PROACTIVE_WORKER_MAX_ATTEMPTS` as their retry budget.
- Read-only deliberation tool use is bounded by
  `ARIEL_PROACTIVE_DELIBERATION_TOOL_ROUNDS`.
- Stale `failed` proactive tasks with attempts remaining recover through the worker by
  returning to `pending`; exhausted tasks remain `dead_letter`.
- No external API call happens inside a database transaction.
- Every external side effect is discoverable during recovery.
- Every case can be replayed from observations, context snapshots, decisions,
  validations, executions, and events.
- Dead letters include enough context for operator remediation.
- Metrics cover observation volume, case volume, deliberation latency, tool-call
  latency, decision mix, speak rate, autonomous action rate, policy denials,
  duplicate suppression, user corrections, false positives, missed opportunities,
  execution failures, and dead letters.

## APIs

- `GET /v1/proactive/observations`: inspect normalized observations.
- `GET /v1/proactive/cases`: inspect cases ordered by update time and state.
- `GET /v1/proactive/cases/{id}`: inspect one case.
- `GET /v1/proactive/cases/{id}/events`: inspect case lifecycle.
- `GET /v1/proactive/cases/{id}/context-snapshots`: inspect context snapshots.
- `GET /v1/proactive/cases/{id}/decisions`: inspect model decisions.
- `GET /v1/proactive/cases/{id}/validations`: inspect policy validations.
- `GET /v1/proactive/cases/{id}/actions`: inspect action plans and executions.
- `GET /v1/proactive/turns`: inspect proactive turns.
- `POST /v1/proactive/cases/{id}/deliberate`: re-run model deliberation for a
  targeted case.
- `POST /v1/proactive/cases/{id}/ack`: acknowledge a proactive turn or case.
- `POST /v1/proactive/cases/{id}/correct`: record correction feedback.
- `POST /v1/proactive/cases/{id}/stop-pattern`: reduce future intervention for a
  matching pattern.
- `POST /v1/proactive/cases/{id}/more-aggressive`: increase future intervention
  for a matching pattern.
- `GET /v1/proactive/autonomy-scopes`: inspect active scopes.
- `POST /v1/proactive/autonomy-scopes`: grant a scope.
- `DELETE /v1/proactive/autonomy-scopes/{id}`: revoke a scope.

## Final State

- The proactive system is AI-deliberative by default.
- Deterministic attention ranking is deleted.
- `attention_*` records, endpoints, serializers, task types, and tests are gone.
- Events and ambient observations open proactive cases.
- Every case receives a model-authored structured decision unless policy or
  context assembly fails closed.
- Speaking first is a first-class assistant-originated turn.
- Autonomous action is a first-class execution path for approved scopes.
- User feedback changes future model deliberation through durable learning
  records.
- Policy validation remains the only authorization boundary for side effects.
- Discord and future channels render proactive turns but do not own decisions.
- Inspection APIs can explain every proactive turn and autonomous action.
- There is no old notification threshold, rank score, delivery decision enum,
  compatibility endpoint, compatibility migration, or legacy task path.

## Acceptance Criteria

- A calendar flight event plus current travel time and user buffer preference
  causes Ariel to speak first when leaving is urgent.
- The airport proactive turn is stored with `origin="proactive"`, no fake user
  message, evidence refs, context snapshot id, decision id, and delivery state.
- A duplicate calendar webhook does not duplicate the case, decision, turn, or
  action.
- A stale travel-time tool result causes the model to refresh context or wait,
  not speak from stale evidence.
- A routine low-value email thread can produce an `ignore` decision with stored
  rationale and no user-facing turn.
- An email that contains prompt injection cannot grant authority, suppress policy,
  change memory, or execute an action.
- A user-approved "reply to routine scheduling emails" autonomy scope lets Ariel
  draft and send an exact routine reply without asking.
- The same scheduling action is denied when the target, payload, or recipient is
  outside the granted scope.
- A denied `act_now` decision records `needs_user_authority` or `denied` and does
  not silently fall back to deterministic notification behavior.
- A configured ambient Discord observation can create a case from a commitment the
  user made in conversation.
- A configured CI failure stream can create a case, deliberate with repo context,
  and open an allowed internal follow-up action when scoped.
- A failed autonomous external call is replayable and does not double-send on
  retry.
- Feedback "stop interrupting me about this" creates a durable learning record
  visible to future deliberations.
- Feedback "do this automatically next time" can propose an autonomy scope, but
  does not grant one unless the user confirms that scope.
- Proactive delivery controls include acknowledge, correct, stop pattern, make
  more aggressive, inspect why, and undo when the action supports undo.
- Every proactive case can answer "why did you speak or act?" using only stored
  records.
- All old `attention_*` APIs return 404 or are removed from routing.
- Old attention worker task types are invalid and dead-lettered as unsupported
  deployment leftovers.
- Tests fail if deterministic rank score, priority threshold, or delivery enum
  code remains reachable as the proactive decision maker.
- Integration tests cover event-triggered deliberation, ambient observation,
  autonomous action, policy denial, retry idempotency, prompt injection, feedback
  learning, and inspection APIs.

## Non-Goals

- Do not build scheduled prompts.
- Do not build daily briefings.
- Do not build deterministic attention ranking.
- Do not build compatibility with old proactive attention APIs or task payloads.
- Do not use model-provider hosted memory as Ariel's proactive source of truth.
- Do not build a generic end-user automation rule builder.
- Do not support sensors that are not explicitly configured.
- Do not execute money movement, legal filing, medical treatment decisions,
  credential disclosure, destructive deletion, or public posting until each class
  has explicit domain policy, autonomy scope shape, and acceptance tests.
- Do not use mobile push, SSE, WebSocket, or LISTEN/NOTIFY as the correctness
  mechanism.

## Key Decisions

- AI deliberation owns proactive judgment.
- Deterministic code owns validation, authorization, persistence, replay, and
  side-effect correctness.
- Proactive cases replace attention items as the user-visible lifecycle object.
- Proactive decisions replace rank snapshots and delivery decisions.
- Proactive turns replace notification-first messaging.
- Ambient observers write observations only.
- Autonomy scopes, not one-off confirmations, define what Ariel can do freely.
- The runtime validates exact model-authored action payloads.
- Invalid model output fails closed without deterministic fallback.
- Feedback changes future model deliberation through durable learning records.
- Hard cutover deletes old code paths instead of hiding them behind flags.

## File Plan

- `docs/proactive-ai-deliberation-cutover.md`: canonical cutover spec.
- `docs/index.md`: link this spec and remove the old attention-ranking spec.
- `docs/production-runbook.md`: add proactive case, decision, action, and
  recovery playbooks.
- `.env.example`: add model, tool, sensor, autonomy, and proactive worker
  settings.
- `alembic/versions/*_proactive_ai_deliberation_cutover.py`: drop old attention
  records and add proactive observation, case, context, decision, validation,
  turn, action, autonomy, feedback, and learning records.
- `src/ariel/persistence.py`: replace attention ORM records and serializers with
  proactive records and serializers.
- `src/ariel/response_contracts.py`: replace attention response contracts with
  proactive case, decision, turn, action, feedback, and autonomy contracts.
- `src/ariel/proactivity.py`: own context assembly, model deliberation calls,
  decision parsing, policy validation, case routing, ambient observation
  derivation, feedback learning orchestration, and proactive controls.
- `src/ariel/sync_runtime.py`: convert provider deltas into proactive
  observations and case updates.
- `src/ariel/app.py`: replace attention endpoints with proactive inspection,
  deliberation, controls, autonomy scope, and feedback endpoints.
- `src/ariel/worker.py`: replace attention task dispatch with proactive
  observation, case, context, deliberation, validation, execution, delivery,
  follow-up, and learning tasks.
- `src/ariel/proactivity.py`: execute policy-authorized proactive action plans
  with exact payload hashes and side-effect receipts.
- `src/ariel/proactivity.py`: validate decisions, taint, autonomy scopes,
  payload hashes, idempotency, and hard safety blocks.
- `src/ariel/memory.py`: provide proactive context sections and feedback-derived
  learning records.
- `src/ariel/discord_bot.py`: deliver proactive turns and controls without owning
  proactive decisions.
- `src/ariel/config.py`: add settings for sensors, deliberation models, tool
  budgets, autonomy defaults, and worker retry budgets.
- `tests/integration/`: replace attention tests with event-triggered
  deliberation, ambient sensing, autonomous action, denial, retry, feedback, and
  inspection acceptance tests.
- `tests/unit/`: add decision contract, policy validation, autonomy scope,
  context assembly, action payload hash, and Discord rendering tests.
- `README.md`: update the product summary after implementation.

## Implementation Plan

1. Delete old attention contracts, tasks, and docs references.
2. Add proactive schema and response contracts.
3. Add observation ingestion and case routing.
4. Add context snapshot assembly.
5. Add model deliberation adapter and strict decision parsing.
6. Add policy validation for decisions and action plans.
7. Add proactive turn creation and Discord delivery.
8. Add autonomy scopes and action execution.
9. Add ambient sensors for existing sources first: Discord, Google sync, captures,
   jobs, approvals, memory commitments, and connector health.
10. Add feedback learning records and future-context injection.
11. Replace tests with the new acceptance suite.
12. Update runbook, README, `.env.example`, and operator recovery docs.

Each implementation step can land in a branch, but no step is production-ready
until old attention ranking is unreachable and the full new acceptance suite
passes.
