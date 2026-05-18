# Maps Expansion Cutover

## Scope

This doc owns the expansion of Ariel's maps vertical beyond the single-leg
read capabilities specified in `docs/modules/maps.md`. It delivers two
workstreams as a hard cutover:

- **Workstream A — Routing depth.** `cap.maps.directions` gains multi-stop
  waypoints, waypoint-order optimization, alternative routes, per-leg breakdown,
  and free-flow (no-traffic) durations. Its contract is replaced, not extended:
  the v1 input/output schemas are deleted.
- **Workstream B — Leave-by reminder.** A new proactive subsystem that watches
  the user's calendar for upcoming located events, computes traffic-aware travel
  time near the event, and surfaces a "leave by HH:MM" notification with an offer
  to add an approval-gated "Leave for X" calendar hold.

It supersedes the s6-pr02 non-goals "traffic-aware route optimization",
"multi-stop planning", and "proactive maps-triggered notifications" recorded in
`docs/modules/maps.md`. Those were initial-slice scoping; this doc removes them.

## Thesis

A jarvis-class assistant should plan a day's stops, not just route A→B, and
should tell the user when to leave without being asked. Both are read-grounded:
the Google Maps Platform is a read/compute API with no write surface, so the
"action" in a leave-by reminder is maps-read evidence feeding Ariel's existing
calendar-write and proactive machinery — not a new maps side effect.

The leave-by reminder is structurally a **timed, self-rescheduling loop**: it
must recompute travel time with live traffic *near* the event, because traffic
computed hours ahead is wrong. Ariel already runs exactly this pattern for work
follow-ups (`work_follow_up_loops`). Leave-by reuses the generic timed-task
primitive (`background_tasks.run_after`) and that loop shape, but owns its own
state table — it is a calendar-trip concern, not a work-commitment concern.

## Cutover Policy

Inherits `docs/schema-consolidation-cutover.md`'s policy.

- Ship as the sequence of PRs in **Implementation Plan**. Each PR is one coherent
  increment; `ruff`, `ruff format --check`, `mypy src tests`, and the full
  `pytest` suite are green at every PR.
- No legacy, no fallbacks, no backward compatibility, no feature flags, no
  dual code paths. Workstream A replaces the `cap.maps.directions` contract
  outright: `maps_directions_query_v1` / `maps_directions_result_v1` and the
  single-route output shape are deleted, and `test_s6_pr02_acceptance.py`'s
  directions tests are rewritten to the v2 contract, not appended to.
- Each schema PR is one Alembic migration with a working `downgrade()`. Foreign
  keys are `ondelete=RESTRICT` per `docs/database.md`. CHECK-enum widenings
  (`background_tasks.task_type`, `notifications.source_type`,
  `ai_judgments.judgment_type`) drop and recreate the constraint in the same
  migration.
- Workstream B is net-new. "No fallback" means: when `ARIEL_MAPS_API_KEY` is
  unset or the Google calendar connector is unavailable, the leave-by subsystem
  is **inert by configuration** — the scan does nothing — not gated by a
  fallback branch inside the evaluator. A maps or calendar failure inside an
  evaluation is a typed terminal state, never a silent degraded reminder.
- Every capability or schema change cites the rule it satisfies: `ai-first.md`,
  `simplicity.md`, `cleanliness.md`, `database.md`.

## Goals

- `cap.maps.directions` answers "plan my errands: home → cleaner → grocery →
  office, best order" in one call, with a per-leg time breakdown.
- `cap.maps.directions` returns alternative routes for a plain A→B query so the
  model can present trade-offs ("I-5 is 20 min; I-405 is 24 but skips downtown").
- Every directions route carries both a traffic-aware duration and a free-flow
  duration, so "traffic adds M min" is derivable without a second call.
- Ariel proactively tells the user when to leave for an upcoming calendar event
  that has a location, computed with live traffic close to departure time.
- The leave-by reminder offers — and on acceptance creates — an approval-gated
  "Leave for X" calendar hold, using the existing `cap.calendar.create_event`.
- The leave-by subsystem is auditable: every travel computation and notify/skip
  judgment is a durable record.

## Non-Goals

- No live turn-by-turn navigation, no continuous rerouting, no live position
  tracking. Ariel is a chat assistant with no GPS stream; the traffic-aware ETA
  is the form-factor-appropriate version of "live traffic" and already exists.
- No maps write actions. The Maps Platform has no write surface; the only
  write in this doc is `cap.calendar.create_event`, unchanged.
- No commute *learning*. Leave-by works strictly off explicit located calendar
  events; Ariel does not infer routines, home/work, or recurring trips.
- No leave-by for all-day events, events with no location, or events whose
  travel mode is not driving. Transit and walking leave-by are an Open Fork.
- No "you still haven't left" re-notification. With no position signal, the
  reminder fires once near departure and is terminal.
- No leave-by feedback/snooze/learning loop in v1. An Open Fork.
- No change to the proactive ambient/case pipeline, the work-follow-up loop, or
  any calendar capability contract.

---

# Workstream A — Routing Depth

## A.1 Current state to replace

`cap.maps.directions` (`capability_registry.py`) is single-leg: input
`{origin, destination, travel_mode}`, output one route's `distance_meters` /
`duration_seconds` plus a one-entry `results[]`. `_validate_maps_directions_input`
(`~393`), `_maps_route_candidate` (`~2036`, returns the first route only),
`_build_maps_route_result` (`~2063`), `_execute_maps_directions` (`~2179`), the
`_MAPS_ROUTES_FIELD_MASK` constant (`~1915`), and `_maps_directions_source_url`
(`~2054`) all encode the single-leg, single-route assumption. Schemas
`maps_directions_query_v1` / `maps_directions_result_v1`.

The Routes API call already targets `POST routes.googleapis.com/directions/v2:computeRoutes`
and already supports everything below; only the request body, field mask, and
response handling are narrow.

## A.2 Target behavior

- A directions request may carry up to ten ordered `waypoints` between origin
  and destination. With `optimize_order: true`, Google reorders the waypoints
  for the shortest total trip and the output reports the chosen order.
- A directions request with no waypoints returns up to three routes; `routes[0]`
  is Google's recommended route, the rest are alternatives. Waypoint requests
  return a single route (the Routes API does not compute alternatives with
  intermediates).
- Each route reports total distance/duration, a free-flow `static_duration`, a
  provider route descriptor, the effective ordered stop list, and a per-leg
  breakdown.
- A missing origin or destination still produces the typed clarification
  (`maps_origin_required` / `maps_destination_required`); maps still never
  infers location.

## A.3 Capability contract — `cap.maps.directions` v2

`impact_level="read"`, `policy_decision="allow_inline"`, no approval path —
unchanged. `contract_metadata`: `input_schema="maps_directions_query_v2"`,
`output_schema="maps_directions_result_v2"`, `idempotency="deterministic_read"`.
`allowed_egress_destinations=(_MAPS_ROUTES_HOST,)` — unchanged.

**Input — `maps_directions_query_v2`** (keys a subset of the following; any
other key → `schema_invalid`):

| Field | Type | Rules |
|---|---|---|
| `origin` | string \| absent | when present: non-empty, ≤320 chars; absent → `maps_origin_required` clarification at execute |
| `destination` | string \| absent | same as `origin`; absent → `maps_destination_required` |
| `travel_mode` | enum | `driving` \| `walking` \| `bicycling` \| `transit`; default `driving` |
| `waypoints` | list[string] | optional, default `[]`; each non-empty, ≤320 chars; **≤10 items** (`_MAPS_MAX_WAYPOINTS`) |
| `optimize_order` | bool | optional, default `false`; lets Google reorder `waypoints` |

**Output — `maps_directions_result_v2`:**

```
{
  "origin": str, "destination": str,
  "waypoints": [str, ...],            # as supplied
  "travel_mode": str,
  "retrieved_at": str,                # RFC3339 Z
  "uncertainty": "insufficient_evidence" | null,
  "routes": [                         # 1..3 entries; 1 when waypoints present; routes[0] is recommended
    {
      "distance_meters": int,
      "duration_seconds": int,                  # traffic-aware for driving
      "static_duration_seconds": int | null,    # free-flow; traffic delta = duration - static
      "description": str | null,                # provider route descriptor
      "stops": [str, ...],                       # effective travel order: [origin, *waypoints_in_order, destination]
      "legs": [                                  # len == len(stops) - 1, aligned to stops
        {"distance_meters": int, "duration_seconds": int, "static_duration_seconds": int | null}
      ],
      "source": str                             # Google Maps deep link
    }
  ],
  "results": [                        # one citation per route
    {"title": str, "source": str, "snippet": str, "published_at": null}
  ]
}
```

`uncertainty="insufficient_evidence"` and `routes=[]`/`results=[]` when the
Routes API returns no route.

## A.4 Routes API design

Request body to `v2:computeRoutes`:

```
{
  "origin":      {"address": <origin>},
  "destination": {"address": <destination>},
  "intermediates": [{"address": w}, ...],     # omitted when waypoints == []
  "travelMode": <DRIVE|WALK|BICYCLE|TRANSIT>,
  "routingPreference": "TRAFFIC_AWARE",        # DRIVE only
  "optimizeWaypointOrder": true,               # only when optimize_order and waypoints present
  "computeAlternativeRoutes": true             # only when waypoints == []
}
```

`optimizeWaypointOrder` and `computeAlternativeRoutes` are mutually exclusive at
the API level (alternatives are not produced with intermediates); the request
builder sets exactly one or neither, never both.

Field mask header `X-Goog-FieldMask`:
`routes.distanceMeters,routes.duration,routes.staticDuration,routes.description,routes.legs.distanceMeters,routes.legs.duration,routes.legs.staticDuration,routes.optimizedIntermediateWaypointIndex`.

Response handling: when `optimizeWaypointOrder` was set, each route's
`optimizedIntermediateWaypointIndex` gives the reordered waypoint indices — the
builder applies it to produce `stops` in effective order; otherwise `stops` is
`[origin, *waypoints, destination]`. `legs[i]` aligns to `stops[i] → stops[i+1]`.
`staticDuration` (route and leg) is a protobuf duration string parsed by the
existing `_normalize_int_like`.

## A.5 Architecture and code

`src/ariel/capability_registry.py`:

- `_validate_maps_directions_input` (`~393`) — rewritten: accept and validate
  `waypoints` (list, ≤`_MAPS_MAX_WAYPOINTS`, each a non-empty ≤320-char string)
  and `optimize_order` (bool); normalize `waypoints` to a stripped tuple. The
  normalized payload always carries all five keys.
- `_MAPS_ROUTES_FIELD_MASK` (`~1915`) — replaced with the v2 field mask above.
  New constants `_MAPS_MAX_WAYPOINTS = 10`, `_MAPS_MAX_ALTERNATIVE_ROUTES = 3`.
- `_maps_route_candidate` (`~2036`) → `_maps_route_candidates` — returns the
  list of route dicts (capped at `_MAPS_MAX_ALTERNATIVE_ROUTES`), not the first.
- `_build_maps_route_result` (`~2063`) — rewritten to build one v2 route object:
  totals, `static_duration_seconds`, `stops` (applying
  `optimizedIntermediateWaypointIndex`), `legs`, `source`. Per-leg parsing of
  `routes.legs[].distanceMeters/duration/staticDuration`.
- `_maps_directions_source_url` (`~2054`) — gains `waypoints`; emits the Google
  Maps `dir` deep link `&waypoints=A|B|...` segment.
- `_execute_maps_directions` (`~2179`) — rewritten: build the request body per
  A.4, call `_maps_request_with_retry`, map the route list through
  `_build_maps_route_result`, assemble the v2 output. Origin/destination
  clarification (`maps_origin_required`/`maps_destination_required`) unchanged.
- The `cap.maps.directions` `CapabilityDefinition` — `contract_metadata`
  schema ids → `_v2`.

Unchanged: the retry/backoff layer, `_raise_for_maps_status`,
`_maps_response_json`, egress declaration and allowlist, the `search_places`
capability.

## A.6 Files — Workstream A

**Edited**

- `src/ariel/capability_registry.py` — the functions in A.5.
- `tests/integration/test_s6_pr02_acceptance.py` — directions tests rewritten to
  the v2 contract; new tests for multi-stop, `optimize_order`, alternative
  routes, per-leg breakdown, `static_duration_seconds`, and `waypoints`
  validation rejection.
- `docs/modules/maps.md` — the directions capability section rewritten for v2;
  out-of-scope list updated.
- `README.md` — the maps section: directions capabilities described as
  multi-stop and alternative-route capable.

---

# Workstream B — Leave-By Reminder

## B.1 Target behavior

### User-facing

For a calendar event with a resolvable location, Ariel sends a Discord
notification roughly twenty minutes before the user needs to leave:

> Leave by **1:35 PM** for *Dentist — Dr. Okafor* (2:00 PM). 22-min drive from
> your last meeting; traffic is adding 7 min. Reply "add a hold" and I'll block
> 1:35–2:00 on your calendar.

If the user accepts, Ariel proposes an approval-gated `cap.calendar.create_event`
for a "Leave for Dentist — Dr. Okafor" hold spanning the departure window. The
user approves it through the normal approval flow.

A trivial trip (a short walk, a few minutes' drive) yields no notification — the
leave-by subagent judges it not worth interrupting for.

### System

1. A worker-owned recurring scan finds upcoming located calendar events and
   creates one `leave_by_reminders` row per event.
2. Each row runs a timed loop: it wakes ~2 hours before the event, computes
   traffic-aware travel time, and reschedules itself to wake again ~20 minutes
   before the computed departure time.
3. On the near-departure wake it recomputes with fresh traffic, then a subagent
   decides notify-or-skip and authors the message.
4. A notify decision writes a `notifications` row and delivers it to Discord.

## B.2 Final architecture

Four deterministic components plus one AI subagent. New module
`src/ariel/leave_by.py` owns all of B except the schema and worker dispatch —
`proactivity.py` is already a god file (`cleanliness.md`); leave-by does not
enter it.

**1. Detection — `process_leave_by_scan_due`** (`leave_by.py`). A worker-owned
recurring task on the cadenced-task mechanism used by ambient interpretation
(re-enqueues itself with `run_after = now + ARIEL_LEAVE_BY_SCAN_INTERVAL_SECONDS`,
default 1800). Inert unless `ARIEL_MAPS_API_KEY` is set and the Google connector
is calendar-ready. It queries `google_provider_objects`
(`object_type="calendar_event"`, `status="active"`) for events starting within
`LEAVE_BY_SCAN_HORIZON` (24 h) that are timed (not all-day), have a non-empty
`metadata_json["location"]`, and have no `leave_by_reminders` row. For each it
inserts a `scheduled` row and enqueues a `leave_by_evaluate_due` task with
`run_after = max(now, event_start − LEAVE_BY_INITIAL_LOOKAHEAD)`
(`LEAVE_BY_INITIAL_LOOKAHEAD` = 2 h). It also reconciles rows whose backing event
moved or was cancelled (bump `version`, reschedule or mark `cancelled`).

**2. The evaluate loop — `process_leave_by_evaluate_due`** (`leave_by.py`).
Loads the row; if the task's `version` ≠ the row's `version`, or the row is
terminal, it no-ops (stale-task discard, the work-loop pattern). Otherwise it
re-reads the backing event, resolves the origin (B.5), computes travel time via
`cap.maps.directions`, computes `leave_by_at`, and either reschedules (plan pass)
or runs the subagent and notifies (notify pass) — see B.4.

**3. The leave-by subagent — `leave_by_evaluation`** (B.6). A tools-free model
call deciding `notify` vs `skip` and authoring the message. Audited as one
`ai_judgments` row.

**4. Notification.** A `notifications` row (`source_type="leave_by"`) delivered
by the existing `deliver_discord_notification` worker task. No new delivery
code.

## B.3 Data model

New table `leave_by_reminders` (ORM `LeaveByReminderRecord` in
`persistence.py`). One row per upcoming located calendar event.

| Column | Type | Notes |
|---|---|---|
| `id` | text PK | `lbr_*` |
| `provider_account_id` | text NOT NULL | the Google account |
| `calendar_id` | text NOT NULL | |
| `event_id` | text NOT NULL | |
| `event_summary` | text | for the message |
| `event_location` | text NOT NULL | the trip destination; rows exist only for located events |
| `event_start_at` | timestamptz NOT NULL | denormalized for scheduling |
| `state` | text NOT NULL | CHECK ∈ `scheduled, computed, notified, skipped, cancelled, failed` |
| `version` | integer NOT NULL | default 1; bumped on every mutation |
| `next_check_at` | timestamptz | null when terminal |
| `resolved_origin` | text | set on first compute |
| `last_duration_seconds` | integer | last traffic-aware travel time |
| `last_static_duration_seconds` | integer | last free-flow travel time |
| `leave_by_at` | timestamptz | computed departure time |
| `notification_id` | text FK → `notifications(id)` `ondelete=RESTRICT` | set on notify |
| `created_at` / `updated_at` | timestamptz NOT NULL | |

`UNIQUE (provider_account_id, calendar_id, event_id)` — one reminder per event;
the natural key, no FK into `google_provider_objects` (which is churned by
sync). `state` lifecycle: `scheduled → computed → (notified | skipped)`, with
`cancelled` (event gone/moved past) and `failed` (maps unrecoverable) as
terminal escapes.

Migrations also widen three CHECK enums (drop + recreate in the same migration):

- `background_tasks.task_type` += `leave_by_scan_due`, `leave_by_evaluate_due`.
  Tasks use the generic JSONB `payload` (`{"reminder_id", "version"}`), not
  typed columns.
- `notifications.source_type` += `leave_by`. The existing
  `ck_notification_proactive_shape` CHECK constrains only `proactive_turn`, so a
  `leave_by` notification with null `proactive_case_id`/`proactive_decision_id`
  is already legal.
- `ai_judgments.judgment_type` += `leave_by_evaluation`.

## B.4 The evaluate loop

`process_leave_by_evaluate_due(reminder_id, version)`:

1. **Guard.** Load the row. Stale `version` or terminal `state` → no-op.
2. **Re-verify the event.** Re-read `google_provider_objects`. Event missing,
   cancelled, or already started → `state="cancelled"`, `next_check_at=NULL`.
3. **Resolve origin** (B.5). Unresolvable → `state="skipped"`, terminal.
4. **Compute travel.** Build `cap.maps.directions` input
   `{origin, destination: event_location, travel_mode: "driving"}`,
   `validate_input`, then `execute_capability`. Read `routes[0].duration_seconds`
   and `routes[0].static_duration_seconds`. A transient maps failure
   (`provider_timeout`/`_rate_limited`/`_upstream_failure`/`_network_failure`)
   within a bounded retry budget → reschedule a near retry. A persistent or
   non-transient failure (`provider_permission_denied`, `maps_location_not_found`,
   budget exhausted) → `state="failed"`, terminal.
5. **Compute** `leave_by_at = event_start_at − duration_seconds − LEAVE_BY_ARRIVAL_BUFFER`
   (`LEAVE_BY_ARRIVAL_BUFFER` = 5 min). Persist `resolved_origin`,
   `last_duration_seconds`, `last_static_duration_seconds`, `leave_by_at`.
6. **Branch on phase.**
   - **Plan pass** — `now < leave_by_at − LEAVE_BY_NOTIFY_LEAD`
     (`LEAVE_BY_NOTIFY_LEAD` = 20 min): `state="computed"`, bump `version`, set
     `next_check_at = leave_by_at − LEAVE_BY_NOTIFY_LEAD`, enqueue the next
     `leave_by_evaluate_due`. No notification.
   - **Notify pass** — otherwise: run the leave-by subagent (B.6). `notify` →
     insert a `notifications` row (`dedupe_key="leave-by:{id}:{version}"`),
     enqueue `deliver_discord_notification`, set `notification_id`,
     `state="notified"`. `skip` → `state="skipped"`. Either way terminal,
     `next_check_at=NULL`.

The loop wakes about twice per event. `version` + the
`leave-by-evaluate:{id}:{version}:{scheduled_for}` task idempotency key make a
moved event's stale tasks no-op, exactly as `work_follow_up_evaluate_due` does.

## B.5 Origin resolution

Deterministic, in the evaluator, per the user's chosen policy:

1. **Preceding event.** Query `google_provider_objects` for the same
   `provider_account_id` calendar event whose end is the latest before
   `event_start_at`, with a non-empty `metadata_json["location"]`, where the gap
   `event_start_at − preceding_end ≤ LEAVE_BY_MAX_ORIGIN_GAP` (3 h). Use its
   location.
2. **Home base.** Else `ARIEL_HOME_ADDRESS` (new config setting).
3. **Neither** → `state="skipped"`. Leave-by from home requires
   `ARIEL_HOME_ADDRESS`; this is documented in the runbook.

## B.6 The leave-by subagent

A tools-free model call (`judgment_type="leave_by_evaluation"`), the
`process_work_follow_up_evaluate_due` subagent pattern. Per `ai-first.md`, the
deterministic evaluator gathers the evidence (travel time, traffic delta,
departure time); the model owns the judgment — is this worth interrupting for,
and the wording.

Input context: event summary/location/start, resolved origin, `duration_seconds`,
`static_duration_seconds`, traffic delta, `leave_by_at`, the current time.
Output (strict JSON): `{"decision": "notify" | "skip", "urgency":
"normal" | "high", "message": str}`. `message` is the user-facing notification
body and includes the leave-by offer (B.7). A trivial or already-past trip is
the model's `skip`. Invalid model output fails closed (`state="failed"`) — no
deterministic fallback message (`ai-first.md`).

## B.7 Composition with other systems

- **maps.** The evaluator calls `cap.maps.directions` v2 through
  `execute_capability` — the same path worker code already uses for capabilities.
  Leave-by needs `static_duration_seconds`, which Workstream A adds; B therefore
  sequences after A.
- **calendar (read).** Detection and the preceding-event lookup read
  `google_provider_objects`, populated by the existing `sync_runtime.py` calendar
  sync — no calendar capability call, no model. The action-runtime
  `cap.calendar.list` evidence path drops `location`; the sync path keeps it, so
  `google_provider_objects` is the correct source.
- **calendar (write).** The "Leave for X" hold is **not** new code. The leave-by
  notification offers it in text; when the user accepts in a normal turn, the
  model proposes `cap.calendar.create_event` (`{title, start_time: leave_by_at,
  end_time: event_start_at, ...}`), which is already `requires_approval` with
  exact-payload-hash binding. Its write-authority anchor is `source_evidence_id`
  pointing at the triggering event's provider evidence.
- **notifications.** Reuses the consolidated `notifications` table,
  `deliver_discord_notification`, and `dedupe_key`. No new delivery path.
- **proactive layer.** Leave-by does **not** flow through `proactive_cases` or
  the ambient pipeline — that pipeline is tick-based with coarse recheck buckets.
  Leave-by is a timed loop, parallel to (not part of) the work-follow-up loop.

## B.8 Files — Workstream B

**Created**

- `src/ariel/leave_by.py` — detection scan, the evaluate loop, origin
  resolution, the leave-by subagent call, notification creation.
- `alembic/versions/<ts>_leave_by_reminders.py` — create `leave_by_reminders`;
  widen the three CHECK enums.
- `docs/modules/leave-by.md` — the steady-state module doc; registered in
  `docs/modules/index.md`.
- `tests/integration/test_leave_by_acceptance.py` — end-to-end coverage.

**Edited**

- `src/ariel/persistence.py` — `LeaveByReminderRecord`; the three CHECK-enum
  changes.
- `src/ariel/worker.py` — dispatch `leave_by_scan_due` and
  `leave_by_evaluate_due` to `leave_by.py`.
- `src/ariel/config.py` — `ARIEL_HOME_ADDRESS` (`str | None`),
  `ARIEL_LEAVE_BY_SCAN_INTERVAL_SECONDS` (positive float/int, default 1800);
  both wired into the existing settings validators.
- `src/ariel/db.py` — `leave_by_reminders` added to `REQUIRED_TABLES`.
- `docs/production-runbook.md` — `ARIEL_HOME_ADDRESS` and the leave-by
  subsystem's operational behavior.
- `.env.example` — `ARIEL_HOME_ADDRESS`, `ARIEL_LEAVE_BY_SCAN_INTERVAL_SECONDS`.

---

# Key Decisions

### Workstream A extends `cap.maps.directions`; it is not a new capability

Multi-stop, alternatives, and legs are all "directions". A separate
`cap.maps.route_plan` would be an interchangeable duplicate of the same
capability, which `simplicity.md` forbids. The id stays `cap.maps.directions`;
the contract hard-cuts to v2 and v1 is deleted.

### Alternatives are always requested, with no input flag

`computeAlternativeRoutes` costs nothing extra on the Routes API and applies
only to no-waypoint queries. Always requesting it and letting the model decide
whether to present alternatives is one fewer input field and one fewer code
path than a `want_alternatives` flag (`simplicity.md`).

### Free-flow duration ships in Workstream A, not Workstream B

`static_duration_seconds` ("traffic adds M min") is useful for every directions
answer, not just leave-by. It belongs to the directions contract; B consumes it.
This also means one Routes call yields both durations — B never makes a second
call for the no-traffic figure.

### Leave-by is a timed loop with its own table, not a proactive case and not a work-follow-up loop

The ambient/case pipeline cannot express "recompute near event time" — its
recheck intervals are a coarse fixed allowlist. The work-follow-up loop *can*
(`next_check_at`), but it is owned by work commitments; bending `loop_kind` and
its owner CHECK to also mean "calendar trip" muddies that table's ownership
(`cleanliness.md`: one concern, one owner). Leave-by reuses the genuinely
generic primitive — `background_tasks.run_after` — and the loop *shape*
(`version` + `next_check_at` + stale-task discard), but owns `leave_by_reminders`.

### Travel time is computed by the deterministic evaluator, not the model

The proactive deliberation model is handed no tools and denies tool calls. More
fundamentally, `ai-first.md` makes capability execution a rail and judgment the
model's: the evaluator gathers the evidence (the maps call), the subagent judges
(notify-or-skip, wording). The subagent is therefore also tools-free.

### Detection is a periodic sweep, not a calendar-sync hook

A sync hook fires only on calendar deltas, so events already present when
leave-by ships, or synced before entering the horizon, would be missed without
a backfill. A horizon sweep every 30 minutes covers backfill, new events, and
moved events uniformly with one mechanism. Reminders fire hours ahead, so a
30-minute detection latency is immaterial.

### The "Leave for X" hold adds no calendar code

`cap.calendar.create_event` already accepts `title`/`start_time`/`end_time` and
is approval-gated with payload-hash binding. The leave-by notification offers
the hold in text; acceptance is a normal reactive turn through the existing
approval flow. Per `ai-first.md`, the model composes the write — leave-by does
not pre-stage an action plan or add approval UI.

### The leave-by subsystem is inert by configuration, never by a fallback branch

With no `ARIEL_MAPS_API_KEY` or no calendar connector, the scan finds nothing
and creates no rows — there is no "maps unavailable" branch inside the
evaluator. A maps or calendar failure mid-evaluation is a typed terminal state
(`failed`), never a silent or degraded reminder (`cleanliness.md`,
`correctness.md`).

# Implementation Plan

Five PRs. Each is independently shippable and leaves the system coherent; each
runs `ruff`, `mypy src tests`, and the full `pytest` suite green.

### PR 1 — `cap.maps.directions` v2 (Workstream A)

A.5 capability changes; the directions tests in `test_s6_pr02_acceptance.py`
rewritten to the v2 contract with new multi-stop / `optimize_order` /
alternatives / legs / `static_duration` / waypoint-validation tests;
`docs/modules/maps.md` and `README.md` updated. Self-contained.

### PR 2 — Leave-by schema

`leave_by_reminders` table + `LeaveByReminderRecord`; the three CHECK-enum
widenings; the Alembic migration with `downgrade()`; `db.py` `REQUIRED_TABLES`;
the new config settings and their `.env.example` entries. Tests cover the
migration up and down, the ORM model, and the new settings, and pass. No
behavior yet — the table is dormant. `leave_by.py` is created in PR 3, where
it first holds behavior.

### PR 3 — Leave-by detection and the evaluate loop

`process_leave_by_scan_due` and `process_leave_by_evaluate_due`: the scan, the
loop, origin resolution, the `cap.maps.directions` call, the leave-by
computation, and the plan-pass reschedule. The notify pass terminates the loop
at `state="computed"` — no subagent, no notification yet. Worker dispatch for
both task types. Tests cover detection, origin resolution, the plan/notify
phase split, stale-task discard, and the `cancelled`/`skipped`/`failed` paths.

### PR 4 — Leave-by subagent and notification

The `leave_by_evaluation` subagent; the notify pass wired to it; the
`notifications` row, dedupe, and Discord delivery; the leave-by offer text.
`computed` ceases to be terminal — the notify pass now runs the subagent and
emits `notified`/`skipped`. Tests cover the notify/skip decision, the
notification contract, dedupe, and fail-closed on invalid model output.

### PR 5 — Acceptance and docs

`tests/integration/test_leave_by_acceptance.py` — end-to-end: a synced located
event through scan → loop → notification, and the accept-the-hold turn through
`cap.calendar.create_event`. `docs/modules/leave-by.md` created and registered
in `docs/modules/index.md`; the runbook documents the leave-by subsystem.

# Acceptance Criteria

- `cap.maps.directions` accepts up to ten `waypoints` and an `optimize_order`
  flag; with `optimize_order` the output `stops` reflect Google's chosen order.
- A no-waypoint directions call returns up to three `routes`; `routes[0]` is the
  recommended route. A waypoint call returns exactly one route.
- Every route reports `distance_meters`, `duration_seconds`,
  `static_duration_seconds`, `description`, `stops`, `legs` (aligned to `stops`),
  and a `source` deep link that includes the waypoints.
- `maps_directions_query_v1` / `maps_directions_result_v1` and the single-route
  output shape are absent from `capability_registry.py`; no v1 directions test
  remains.
- A located, timed calendar event starting within 24 h produces exactly one
  `leave_by_reminders` row; an all-day or location-less event produces none.
- The evaluate loop computes a traffic-aware `leave_by_at`, reschedules itself
  once to within `LEAVE_BY_NOTIFY_LEAD` of departure, and emits one notification.
- A moved event re-detected by the scan bumps `version`; the superseded
  `leave_by_evaluate_due` task no-ops.
- The leave-by notification states the departure time, the drive time, the
  traffic delta, and offers the calendar hold.
- A trivial trip produces no notification (`state="skipped"`).
- With `ARIEL_MAPS_API_KEY` unset, the scan creates no rows and runs no maps
  call; no `leave_by_evaluate_due` task is enqueued.
- An unrecoverable maps failure mid-evaluation yields `state="failed"` and no
  notification — never a degraded reminder.
- Each migration runs up and down; the full `pytest` suite is green at each PR.

# Sequencing

PR 1 → PR 2 → PR 3 → PR 4 → PR 5, strictly. PR 3's evaluator reads the v2
`routes[]` shape and `static_duration_seconds`, so Workstream A precedes the
Workstream B runtime. PR 2 may land any time after PR 1.

# Open Forks

- **Walking and transit leave-by.** v1 is driving-only. Transit leave-by is the
  most valuable extension — the Routes API supports transit with a target
  arrival time — but it needs schedule-aware logic. Recommended: a follow-up
  slice once driving leave-by is proven.
- **One-click "add hold" button.** v1's offer is notification text; acceptance
  is a reactive turn. A Discord action button that stages the approval-gated
  `cap.calendar.create_event` directly is a UX win but adds notification-UI
  machinery. Recommended deferred.
- **Home address source.** v1 uses the `ARIEL_HOME_ADDRESS` config setting.
  Storing it instead as an AI-maintained memory fact would let the user set it
  conversationally, but the deterministic evaluator cannot cleanly key-query the
  flat fact store. Recommended: keep the config setting; revisit if memory gains
  a keyed-fact lookup.
- **Leave-by feedback.** v1 has no snooze or "stop reminding me" loop. If
  reminders prove noisy, fold leave-by outcomes into the existing proactive
  feedback machinery. Recommended deferred until there is signal.
