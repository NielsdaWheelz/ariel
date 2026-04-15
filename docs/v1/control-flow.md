# Control Flow

## Scope

This document covers exhaustive branching and race-safety rules.

## Exhaustiveness

- When branching on a value with a known finite set of possibilities, use exhaustive matching. That means that adding a new possibility to the producer of the value should cause a type error in the consumer until it explicitly handles that possibility. If new possibilities could possibly be added to the consumer without creating type errors, this rule has been violated.
- Good patterns:
  - Python `match` statement with `assert_never` from `typing` as the standard pattern for exhaustive matching.
  - `assert_never` is the standard compile-time and runtime exhaustiveness check for unreachable branches.
  - `typing.assert_type` is the standard compile-time assertion for narrowed finite variants.
- This applies to errors as well. Do not erase finite error channels with catch-all handlers such as bare `except Exception`, `except BaseException`, or `contextlib.suppress`.
- Usually, the best way to handle errors is with specific `except` clauses matching concrete exception classes.

## Races

- Do not race a coroutine that performs a destructive or non-idempotent operation unless losing the result is acceptable.
- `asyncio.wait` with `return_when=FIRST_COMPLETED` and `asyncio.TaskGroup` with cancellation discard the losing result.
- If the losing coroutine performed an irreversible side effect, the side effect is committed but the result is lost.
- When concurrent coroutines need to coordinate around destructive operations, route signals through a single serialization point.
