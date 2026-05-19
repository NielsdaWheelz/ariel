# Maps Expansion Cutover

## Scope

This doc owns the expansion of Ariel's maps vertical beyond the single-leg
read capabilities specified in `docs/modules/maps.md`. It delivers routing
depth as a hard cutover: `cap.maps.directions` gains multi-stop waypoints,
waypoint-order optimization, alternative routes, per-leg breakdown, and
free-flow (no-traffic) durations. Its contract is replaced, not extended — the
v1 input/output schemas are deleted.

It supersedes the s6-pr02 non-goals "traffic-aware route optimization" and
"multi-stop planning" recorded in `docs/modules/maps.md`. Those were
initial-slice scoping; this doc removes them.

This doc originally also specified a leave-by reminder subsystem. That
subsystem was implemented and then deleted by the proactivity crystallization
([proactivity-cutover.md](proactivity-cutover.md)): a "leave by HH:MM" reminder
is now an emergent agent behavior built from calendar access, this maps
capability, and `proactive.schedule` on a normal wake, not a coded subsystem.
The leave-by content has been removed from this doc; the routing-depth cutover
recorded here stands.

## Thesis

A jarvis-class assistant should plan a day's stops, not just route A→B. This is
read-grounded: the Google Maps Platform is a read/compute API with no write
surface, so multi-stop planning and alternatives are richer maps-read evidence,
not a new maps side effect.

## Cutover Policy

Inherits `docs/schema-consolidation-cutover.md`'s policy.

- `ruff`, `ruff format --check`, `mypy src tests`, and the full `pytest` suite
  are green at every PR.
- No legacy, no fallbacks, no backward compatibility, no feature flags, no
  dual code paths. The `cap.maps.directions` contract is replaced outright:
  `maps_directions_query_v1` / `maps_directions_result_v1` and the
  single-route output shape are deleted, and `test_s6_pr02_acceptance.py`'s
  directions tests are rewritten to the v2 contract, not appended to.
- Every capability change cites the rule it satisfies: `ai-first.md`,
  `simplicity.md`, `cleanliness.md`, `database.md`.

## Goals

- `cap.maps.directions` answers "plan my errands: home → cleaner → grocery →
  office, best order" in one call, with a per-leg time breakdown.
- `cap.maps.directions` returns alternative routes for a plain A→B query so the
  model can present trade-offs ("I-5 is 20 min; I-405 is 24 but skips downtown").
- Every directions route carries both a traffic-aware duration and a free-flow
  duration, so "traffic adds M min" is derivable without a second call.

## Non-Goals

- No live turn-by-turn navigation, no continuous rerouting, no live position
  tracking. Ariel is a chat assistant with no GPS stream; the traffic-aware ETA
  is the form-factor-appropriate version of "live traffic" and already exists.
- No maps write actions. The Maps Platform has no write surface.
- No change to any calendar capability contract.

---

# Routing Depth

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

## A.6 Files

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

# Key Decisions

### Routing depth extends `cap.maps.directions`; it is not a new capability

Multi-stop, alternatives, and legs are all "directions". A separate
`cap.maps.route_plan` would be an interchangeable duplicate of the same
capability, which `simplicity.md` forbids. The id stays `cap.maps.directions`;
the contract hard-cuts to v2 and v1 is deleted.

### Alternatives are always requested, with no input flag

`computeAlternativeRoutes` costs nothing extra on the Routes API and applies
only to no-waypoint queries. Always requesting it and letting the model decide
whether to present alternatives is one fewer input field and one fewer code
path than a `want_alternatives` flag (`simplicity.md`).

### Free-flow duration ships in the directions contract

`static_duration_seconds` ("traffic adds M min") is useful for every directions
answer. It belongs to the directions contract, and one Routes call yields both
the traffic-aware and the free-flow duration — no second call for the
no-traffic figure.

# Implementation

One PR: the A.5 capability changes; the directions tests in
`test_s6_pr02_acceptance.py` rewritten to the v2 contract with new multi-stop,
`optimize_order`, alternatives, legs, `static_duration`, and
waypoint-validation tests; `docs/modules/maps.md` and `README.md` updated.
`ruff`, `mypy src tests`, and the full `pytest` suite are green. The change is
self-contained — no schema migration.

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
- The full `pytest` suite is green.
