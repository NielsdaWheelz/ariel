# Keys And Identities

## Scope

This document covers identity and authority naming, branding, sealing, and related naming rules.

## Id

- Meaningless private identity should use UUID-backed `*Id` values.
- `*Id` means private meaningless identity.
- Do not expose `*Id` directly at end-user boundaries.

## Key

- Meaningful identity should use `*Key` values.
- Prefer structured Pydantic models for meaningful identity keys.
- Do not pass raw anonymous `dict[str, Any]` for owned meaningful identity.
- Give owned structured keys a named type and `*Schema` (Pydantic model).
- Do not replace meaningful identity with meaningless UUIDs just because it identifies something.
- Use `json_key_to_string` only when a boundary genuinely requires a canonical string form of a structured key.
- Do not use `json_key_to_string` as a default persistence format.

## Handle

- `*Handle` means outward opaque identity.
- Handles are sealed outward forms of internal identity.
- End-user boundaries prefer handles for outward opaque identity.
- Do not call an outward handle `id`.
- Outward opaque identity should be named as a handle at boundary surfaces.

## Token And ApiKey

- `*Token` and `*ApiKey` mean outward bearer or capability strings.
- Tokens and API keys are authority, not identity pointers.

## Ref

- `*Ref` is only for lower-layer references such as provider-owned or infrastructure-owned pointers.
- Do not use `*Ref` for outward opaque values.
- Do not use `*Ref` for DTO wrappers.

## Specific Names

- Prefer the most specific honest domain name.
- `Id`, `Key`, `Handle`, `Token`, `ApiKey`, and `Ref` are fallback categories, not the only allowed names.
- Prefer a sharper domain term when it captures the semantics more directly than a generic suffix.
- Use the same specific name across boundaries when the concept itself is the same.
- A specific name should still respect the underlying semantics of the fallback category it replaces.

## Brands

- Use validated `NewType` plus Pydantic models for canonical values whose malformedness is knowable locally.
- Use `NewType` for provenance-backed internal IDs, outward handles, outward tokens, and lower-layer refs.
- Outward sealed handles and tokens may extend a validated local wire-text type such as `SealedRefText`.
- Use owned named types and Pydantic models for semantic structured values rather than passing raw anonymous `dict[str, Any]`.
- Add `NewType` branding when the semantics need nominal distinction beyond the structure itself.
- For canonical owned values, prefer a shared `parse_x` and `assume_x` pair next to the owning type.
- `parse_x` may normalize once at ingress, then validate and return the canonical owned value. See [boundaries.md](boundaries.md) for the general ingress rule.
- `assume_x` requires the value to already be canonical and raises a defect otherwise.
- Do not use `parse_x` naming for helpers that convert outward opaque handles or tokens into internal identity or authority. Use names such as `unseal_x` or `resolve_x`.

## Sealing

- Use sealing only at end-user boundaries to hide private IDs and similar internal references.
- User-controlled infrastructure is an end-user boundary.
- Internally, always use private IDs and internal references.
- Successful unseal or resolve is what converts an outward handle or token into the owning internal type.
- Typed FastAPI and RPC schemas may validate outward sealed wire text as `SealedRefText` at the boundary, but entity-specific unseal or resolve still happens later.
- Entity-specific `unseal_x` and `resolve_x` helpers own classification and conversion from outward wire text into private internal types.
- Raw unsealed strings must not escape boundary helpers.
- RPC and FastAPI request schemas may carry typed outward handles and tokens, but they should not unseal directly into private IDs during decode.
- Handler and service code owns unseal, classification, and conversion from malformed outward values into domain errors.
- If an outward opaque value wraps UUID-backed identity, prefer `seal_id` and `unseal_id`.
- If an outward opaque value must wrap meaningful structured identity, seal a canonical JSON string or buffer rather than inventing an ad hoc string encoding.
