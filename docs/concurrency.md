# Concurrency

## Scope

When and how to handle concurrent execution in FastAPI + SQLAlchemy + PostgreSQL. Does not cover retry semantics; see [database.md](database.md). For operation classification, see [operation-types.md](operation-types.md).

## Linearization

- All backend code may execute concurrently across multiple workers/processes.
- An operation is **linearized** when concurrent execution produces results equivalent to some valid serial ordering.
- If concurrent execution or crash-and-retry cannot be explained by such an ordering, that is a bug.
- Every mutation must choose an explicit linearization strategy.

## One-Transaction DB Work

- Reads and writes that fit in one SERIALIZABLE transaction need no extra coordination.
- SERIALIZABLE isolation is the linearization mechanism. PostgreSQL will abort conflicting transactions automatically.
- Handle serialization failures with retry at the request level.
- Do not add advisory locks or `SELECT FOR UPDATE` around a one-transaction mutation unless the operation also linearizes a non-DB side effect.

```python
async with session.begin():
    # SERIALIZABLE handles all concurrency within this block
    result = await session.execute(
        update(Account).where(Account.id == id).values(balance=new_balance).returning(Account)
    )
```

## Check-Then-Act (TOCTOU Safety)

When the operation reads a value, makes a decision, then writes based on that decision:

### Within a Single Transaction

Use `SELECT ... FOR UPDATE` to lock the row between check and act:

```python
async with session.begin():
    row = (await session.execute(
        select(Account).where(Account.id == id).with_for_update()
    )).scalar_one()

    if row.balance < amount:
        raise InsufficientFunds()

    row.balance -= amount
```

### Across Transactions or External Calls

1. Lock the row or acquire an advisory lock.
2. Read the current state.
3. Perform the external call / second transaction.
4. Write the result and release the lock.

Use PostgreSQL advisory locks when the lock target is not a single row:

```python
# Acquire advisory lock (blocks until available)
await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

# Now safe to check-then-act across this session
current = await get_current_state(session, resource_id)
if not current.is_ready:
    raise NotReady()

await external_api.execute(resource_id)
await mark_complete(session, resource_id)
```

## Serializing Concurrent Mutations

### Single-Step: `SELECT FOR UPDATE`

When multiple requests may mutate the same resource concurrently, use `SELECT FOR UPDATE` to serialize them:

```python
async with session.begin():
    resource = (await session.execute(
        select(Resource).where(Resource.id == id).with_for_update()
    )).scalar_one()
    # Only one transaction proceeds at a time for this resource
    resource.state = compute_new_state(resource)
```

### Single-Step: Advisory Lock

When the lock target is a logical key (not a row), use advisory locks:

```python
async with session.begin():
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": hash_to_int(f"resource:{resource_id}")},
    )
    # Serialized region
    ...
```

Advisory lock variants:
- `pg_advisory_xact_lock(key)` -- held until transaction commits/rollbacks. Preferred.
- `pg_advisory_lock(key)` -- held until explicitly released or session ends. Use for cross-transaction coordination.
- `pg_try_advisory_lock(key)` -- non-blocking. Returns `false` if already held.

### Multi-Step: Advisory Lock Across Transactions

For durable multi-step workflows that need serialization on a shared resource:

```python
# Session-level lock (survives across transactions)
await session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
try:
    await step_one(session, payload)
    await session.commit()

    await step_two(session, payload)
    await session.commit()
finally:
    await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
```

## Async Concurrency in FastAPI

- FastAPI `async def` handlers run on the event loop. CPU-bound or blocking work must go to a thread pool.
- Use `asyncio.gather()` for concurrent I/O within a single request only when the operations are independent.
- Do not share a SQLAlchemy `AsyncSession` across concurrent tasks. Each concurrent task needs its own session.
- For background work, use a proper task queue (arq, Celery, or PostgreSQL-backed). Do not use `asyncio.create_task()` for work that must survive process shutdown.

## Summary of Strategies

| Scenario | Strategy |
|---|---|
| Single-transaction DB mutation | SERIALIZABLE isolation (automatic) |
| Check-then-act within one transaction | `SELECT FOR UPDATE` |
| Check-then-act across transactions/APIs | Advisory lock + idempotent steps |
| Serialize access to same row | `SELECT FOR UPDATE` |
| Serialize access to logical resource | `pg_advisory_xact_lock` |
| Multi-step workflow serialization | `pg_advisory_lock` (session-level) |
| Concurrent async I/O in one request | `asyncio.gather()` with separate sessions |
| Background work | Task queue (not `asyncio.create_task`) |
