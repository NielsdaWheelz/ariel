# Maps

## Scope

This document owns the design of Ariel's maps vertical: the `cap.maps.directions`
and `cap.maps.search_places` read capabilities.

## Capabilities

Both are allowlisted `read` capabilities with `allow_inline` policy and no approval
path. They are grounded-retrieval capabilities: each returns a normalized `results[]`
list with citations, consumed by the same synthesis path as web and news search.

- `cap.maps.directions` — multi-stop route guidance: an origin, a destination, up
  to ten ordered waypoints, optional Google-chosen waypoint ordering, alternative
  routes for a plain A→B query, a per-leg breakdown, and a free-flow duration.
- `cap.maps.search_places` — nearby-place discovery within a radius.

`MAPS_CAPABILITY_IDS` in `capability_registry.py` is the single owner of the maps
capability-id set; tool-surface gating and retrieval classification both derive
from it.

## Provider

Maps calls the Google Maps Platform directly over fixed HTTPS hosts:

- `cap.maps.directions` → Routes API, `POST routes.googleapis.com/directions/v2:computeRoutes`.
- `cap.maps.search_places` → Geocoding API (`maps.googleapis.com`) to resolve the
  location text to a center point, then Places API (New) Text Search
  (`POST places.googleapis.com/v1/places:searchText`).

The Routes API and Places API (New) are used, not the legacy Directions and Places
APIs: the legacy APIs are feature-frozen and cannot be enabled on new Google Cloud
projects.

## Credentials

Maps uses a single Google Maps Platform API key, `ARIEL_MAPS_API_KEY` — a plain
server-managed secret, not an OAuth connector. Maps execution is fully isolated from
the Google OAuth connector used by Gmail, Calendar, and Drive: `cap.maps.*` are not
Google connector capabilities and never touch connector readiness or consent state.

The key is not encrypted at rest by Ariel — encrypting an env-var secret with another
env-var secret adds no protection. Key protection is GCP-side: restrict the key to the
three required APIs and to the deployment egress IP. The maps capabilities are exposed
to the model only when `ARIEL_MAPS_API_KEY` is set.

## Radius enforcement

`cap.maps.search_places` hard-bounds every result to the requested `radius_meters`. A
Places `locationBias.circle` only ranks results — it does not exclude them. So each
returned place is additionally haversine-filtered against the geocoded center, and any
place beyond the radius is dropped. The deterministic filter, not the bias, is what
makes the radius a hard bound.

## Retries

Transient failures (timeout, connection error, HTTP 429 or 5xx) are retried within a
bounded attempt budget, with a linearly growing per-attempt timeout matching the
`web.extract` pattern. Non-transient failures are not retried.

## Typed failures

Maps execution maps every expected failure to a stable reason code.

Clarification codes — the assistant must ask the user, not retry or infer:

- `maps_origin_required`, `maps_destination_required`, `maps_location_context_required`
  — a required field is absent.
- `maps_location_not_found` — a location text is present but geocodes to no result.

Maps never infers location from device or IP signals; a missing or unresolvable
location is always an explicit clarification.

Provider/runtime codes — `provider_credentials_missing`, `provider_timeout`,
`provider_network_failure`, `provider_rate_limited`, `provider_upstream_failure`,
`provider_permission_denied`, `provider_request_rejected`, `provider_invalid_payload`.

The model receives the reason code and authors the user-facing recovery or
clarification text; deterministic code does not write that prose.

## Directions contract

`cap.maps.directions` takes `origin`, `destination`, `travel_mode`, an optional
`waypoints` list (each non-empty, ≤320 chars, ≤10 items), and an optional
`optimize_order` flag. A missing `origin` or `destination` is the typed
`maps_origin_required` / `maps_destination_required` clarification; maps never
infers location.

It returns a `routes[]` list — one route when waypoints are supplied, otherwise up
to three, with `routes[0]` Google's recommended route and the rest alternatives
(the Routes API does not compute alternatives alongside intermediate stops). Each
route carries `distance_meters`, a traffic-aware `duration_seconds`, a free-flow
`static_duration_seconds` (so "traffic adds M min" is `duration - static`), a
provider `description`, the effective ordered `stops`
(`[origin, *waypoints, destination]`, reordered when `optimize_order` is set), a
per-leg `legs` breakdown aligned to `stops`, and a Google Maps `source` deep link.
Alongside `routes[]`, `results[]` carries one citation per route for the shared
synthesis path.

## Output shape

`cap.maps.search_places` returns `results[]` where each place carries structured
`address`, `distance_meters`, `rating`, `rating_count`, `open_now`, and
`business_status` fields. Facts are structured fields the model reads directly,
never values packed into a snippet string. Each result's `snippet` is
human-readable citation text (a route descriptor or a place address); citations are
canonical Google Maps URLs.

## Egress

Each capability declares its egress intent and carries a static
`allowed_egress_destinations` allowlist of exactly the Google hosts it calls. Egress
preflight is fail-closed: an undeclared or non-allowlisted destination is blocked
before execution.

## Out of scope

Commute personalization, proactive maps-triggered notifications, and any write-side
maps action. Driving ETAs use the Routes API `TRAFFIC_AWARE` routing preference;
`cap.maps.directions` reorders supplied waypoints and returns alternatives, but it
does not learn routines or infer trips.
