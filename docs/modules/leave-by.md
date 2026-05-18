# Leave-By

## Scope

This document owns the leave-by reminder subsystem: a proactive loop that watches
the calendar for upcoming located events, computes traffic-aware travel time near
departure, and notifies the user when to leave.

The subsystem is `src/ariel/leave_by.py` plus the `leave_by_reminders` table. It
owns two `background_tasks` types — `leave_by_scan_due` and `leave_by_evaluate_due`
— and writes `notifications` (`source_type="leave_by"`) and `ai_judgments`
(`judgment_type="leave_by_evaluation"`) rows. It adds no calendar or maps code.

## Architecture

Four deterministic components and one AI subagent.

- **Detection** — `process_leave_by_scan_due`. A worker-owned recurring scan.
  `worker.py` re-enqueues a `leave_by_scan_due` task every
  `ARIEL_LEAVE_BY_SCAN_INTERVAL_SECONDS`. The scan queries
  `google_provider_objects` for active `calendar_event` rows starting within
  `LEAVE_BY_SCAN_HORIZON`, opens one `scheduled` `leave_by_reminders` row per
  timed located event, and enqueues that row's first `leave_by_evaluate_due` task.
- **The evaluate loop** — `process_leave_by_evaluate_due`. A timed,
  self-rescheduling loop. It re-verifies the event, resolves the origin, computes
  travel via `cap.maps.directions`, computes `leave_by_at`, then runs one of two
  passes. The **plan pass** reschedules the loop to wake near departure; the
  **notify pass** runs the subagent and notifies. The loop wakes about twice per
  event.
- **The leave-by subagent** — `leave_by_evaluation`. A tools-free model call that
  decides notify-or-skip and authors the message. Audited as one `ai_judgments`
  row.
- **Notification.** A `notifications` row delivered by the existing
  `deliver_discord_notification` worker task. No new delivery code.

## The evaluate loop

`process_leave_by_evaluate_due` runs in three transaction-bounded steps:

1. **Guard and re-verify.** Load the row. A stale `version` or a terminal `state`
   is a no-op. Re-read the backing event; an event that is missing, inactive,
   undated, or already started sets `state="cancelled"`. Resolve the origin; an
   unresolvable origin sets `state="skipped"`.
2. **Compute travel.** Outside any transaction, call `cap.maps.directions` with
   `{origin, destination: event_location, travel_mode: "driving"}` through
   `execute_capability`, and read `routes[0].duration_seconds` and
   `static_duration_seconds`.
3. **Persist and branch.** Re-check the guard, persist the computation, and run
   the plan or notify pass.

`leave_by_at = event_start_at − duration_seconds − LEAVE_BY_ARRIVAL_BUFFER`.

The plan pass runs when `now < leave_by_at − LEAVE_BY_NOTIFY_LEAD`: it sets
`state="computed"`, bumps `version`, sets `next_check_at` to
`leave_by_at − LEAVE_BY_NOTIFY_LEAD`, and enqueues the next
`leave_by_evaluate_due` task. The notify pass runs otherwise: it runs the subagent,
then on `notify` writes a `notifications` row and enqueues delivery, or on `skip`
sets `state="skipped"`.

A transient maps failure (`provider_timeout`, `provider_rate_limited`,
`provider_upstream_failure`, `provider_network_failure`) reschedules a near retry
within `LEAVE_BY_MAX_COMPUTE_ATTEMPTS` attempts. A non-transient or budget-exhausted
maps failure sets `state="failed"`.

`version` and the `leave-by-evaluate:{id}:{version}:{scheduled_for}` task
idempotency key make a moved event's superseded tasks no-op.

## Origin resolution

`_resolve_origin` resolves the trip's starting point deterministically:

1. **Preceding event.** The same account's located calendar event whose end is the
   latest before `event_start_at`, where the gap is at most
   `LEAVE_BY_MAX_ORIGIN_GAP`. Use its location.
2. **Home base.** Else `ARIEL_HOME_ADDRESS`.
3. **Neither** — the trip is skipped (`state="skipped"`).

Leave-by from home requires `ARIEL_HOME_ADDRESS`.

## Horizon constants

`leave_by.py` owns the timing constants:

- `LEAVE_BY_SCAN_HORIZON` — 24 h. The scan looks one day out.
- `LEAVE_BY_INITIAL_LOOKAHEAD` — 2 h. The loop first wakes this far before the
  event.
- `LEAVE_BY_NOTIFY_LEAD` — 20 min. The loop's second wake is this far before
  `leave_by_at`; it is the plan/notify split.
- `LEAVE_BY_ARRIVAL_BUFFER` — 5 min. Slack subtracted into `leave_by_at`.
- `LEAVE_BY_MAX_ORIGIN_GAP` — 3 h. The largest preceding-event gap that resolves
  an origin.
- `LEAVE_BY_MAX_COMPUTE_ATTEMPTS` — 3. The transient-maps-failure retry budget.

## Reminder state

`leave_by_reminders.state` is `scheduled → computed → notified | skipped |
cancelled | failed`.

- `scheduled` — the row exists; travel is not yet computed.
- `computed` — the plan pass computed travel and rescheduled. Not terminal.
- `notified` — terminal. The notify pass emitted a notification.
- `skipped` — terminal. The origin was unresolvable, or the subagent judged the
  trip not worth interrupting for.
- `cancelled` — terminal. The backing event was removed, moved past, or lost its
  time or location.
- `failed` — terminal. Maps failed unrecoverably, or the subagent returned invalid
  output.

`version` is bumped on every mutation. `next_check_at` is null when terminal.

## The leave-by subagent

`_run_leave_by_evaluation` is a tools-free model call. The deterministic evaluator
gathers the evidence — event summary, location, start, resolved origin,
`duration_seconds`, `static_duration_seconds`, the traffic delta, `leave_by_at`,
and the current time. The model owns the judgment and the wording.

Output is strict JSON: `{"decision": "notify" | "skip", "urgency": "normal" |
"high", "message": str}`. `message` is the user-facing notification body; it
states the leave-by time, the drive time, and the traffic delta, and offers the
calendar hold. A trivial or already-past trip is the model's `skip`. Invalid or
missing model output fails closed — `state="failed"`, no notification, no
deterministic fallback message.

## Configuration

- `ARIEL_HOME_ADDRESS` — the origin fallback when no preceding located event
  resolves. Optional; when unset, leave-by-from-home trips are skipped.
- `ARIEL_LEAVE_BY_SCAN_INTERVAL_SECONDS` — the detection scan cadence. Default
  1800.

The subsystem is inert unless `ARIEL_MAPS_API_KEY` is set and the Google calendar
connector is connected with the calendar read scope. With either absent, the scan
returns immediately and opens no rows — leave-by is inert by configuration, not by
a fallback branch inside the evaluator.

## Composition

- **maps.** The evaluator calls `cap.maps.directions` through `execute_capability`,
  the same path worker code uses for every capability. It reads the v2 route's
  traffic-aware `duration_seconds` and free-flow `static_duration_seconds`.
- **calendar (read).** Detection and the preceding-event lookup read
  `google_provider_objects`, populated by the existing calendar sync. No calendar
  capability call, no model.
- **calendar (write).** The "Leave for X" hold is not leave-by code. The
  notification offers it in text; when the user accepts in a normal turn, the model
  composes the already-existing approval-gated `cap.calendar.create_event`.
- **notifications.** Reuses the `notifications` table, `deliver_discord_notification`,
  and `dedupe_key` (`leave-by:{id}:{version}`).

## Out of scope

Commute learning, route inference, and home/work inference. Leave-by works strictly
off explicit located calendar events. No leave-by for all-day events, location-less
events, or non-driving travel modes. No live position tracking and no "you still
haven't left" re-notification — the reminder fires once near departure and is
terminal. No snooze or feedback loop.
