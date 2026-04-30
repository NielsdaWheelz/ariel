# Operation Types

## Scope

Classification of database and external operations by complexity, idempotency requirements, and transaction boundaries.

## Purpose

Mutating operations can be interrupted by retries, timeouts, and crashes. A later attempt must finish the same operation without double-applying side effects or drifting to a different result.

Every write operation must declare its complexity class, idempotency strategy, and transaction boundary.

## Complexity Classification

Operations ordered from lightest to heaviest:

| Class | Description | Idempotent | Transaction | Side effects |
|---|---|---|---|---|
| Pure read | Deterministic computation, no DB | n/a | none | none |
| Single read | One SELECT, possibly with joins | n/a | READ ONLY | read-only |
| Streaming read | Cursor or paginated result set | n/a | READ ONLY | read-only |
| Single write | One atomic INSERT/UPDATE/DELETE | required | SERIALIZABLE | exactly 1 |
| Multi-step | Multiple writes across transactions or external calls | required | per-step | multiple |
| Durable workflow | Multi-step with autonomous completion | required | per-step | multiple (presented as 1) |

## Operation Details

### Pure Read

A deterministic, side-effect-free computation. No DB access. Always returns the same result for the same input. Implemented as a plain function.

### Single Read

A read against the database. Use a SQLAlchemy session with a read-only transaction.

```python
async def get_user(session: AsyncSession, user_id: str) -> User:
    result = await session.execute(
        select(User).where(User.id == user_id)
    )
    return result.scalar_one()
```

- Use `execution_options(postgresql_readonly=True)` or an explicit `SET TRANSACTION READ ONLY` when the operation is purely read.
- For point-in-time consistency across multiple reads, run them in a single SERIALIZABLE READ ONLY transaction.

### Streaming Read

A read returning an async iterator or paginated result set. Use server-side cursors for large result sets.

```python
async def list_users(session: AsyncSession) -> AsyncIterator[User]:
    result = await session.stream(select(User))
    async for row in result:
        yield row.User
```

### Single Write

One atomic state change in a single SERIALIZABLE transaction. This is the default and preferred mutation shape.

```python
async def create_order(session: AsyncSession, payload: CreateOrder) -> Order:
    order = Order(**payload.dict())
    session.add(order)
    await session.flush()
    return order
```

Rules:
- One transaction owns the entire mutation.
- SERIALIZABLE isolation handles linearization automatically.
- The handler function receives a session that is committed by the caller/middleware.
- Do not call external APIs inside the transaction body.

#### Idempotency for Single Writes

Use a unique constraint on the idempotency key column. SELECT first, then INSERT inside a SERIALIZABLE transaction:

```python
async with session.begin():
    existing = await session.scalar(
        select(Order).where(Order.idempotency_key == request.idempotency_key)
    )
    if existing:
        return existing

    order = Order(idempotency_key=request.idempotency_key, ...)
    session.add(order)
    await session.flush()
    return order
```

- The `idempotency_key` comes from the client request. Namespace it: `f"{operation_name}:{client_key}"`.
- The caller keeps the key stable across retries of the same logical mutation.
- SERIALIZABLE isolation makes the SELECT-then-INSERT safe against concurrent duplicates; a serialization failure triggers a retry.
- Use `begin_nested()` (savepoint) if you need to catch `IntegrityError` from a concurrent race without aborting the outer transaction.

### Multi-Step Operation

Multiple writes that span more than one transaction or mix DB writes with external API calls.

Rules:
- Each step gets its own transaction.
- Steps must be individually idempotent.
- Use a status column or state machine row to track progress.
- On retry, check which steps completed and resume from the next incomplete step.
- Wrap external calls with idempotency keys on the provider side when available.

```python
async def provision_account(db: AsyncSession, payload: ProvisionPayload) -> None:
    # Step 1: Create DB record (idempotent via unique constraint)
    account = await get_or_create_account(db, payload)
    await db.commit()

    # Step 2: Call external API (idempotent via provider idempotency key)
    await external_api.provision(
        account_id=account.id,
        idempotency_key=f"provision:{account.id}",
    )

    # Step 3: Mark complete (idempotent via status check)
    await mark_provisioned(db, account.id)
    await db.commit()
```

### Durable Workflow

A multi-step operation that will run to completion on its own, even if the original caller crashes. Use a background task system (e.g., Celery, arq, or a PostgreSQL-backed task queue).

- Submit the workflow to the task queue.
- The task runner picks up incomplete work and replays to completion.
- Each step checks prior completion before re-executing.
- Fire-and-forget: enqueue and return immediately. Foreground: enqueue and poll/await the result.

## Read vs Write Separation

- Read handlers must not perform writes. Enforce at the session level with read-only transactions.
- Write handlers may read, but reads inside a write transaction hold locks. Minimize reads inside write transactions.
- Never mix external API calls with DB transactions. Commit the transaction first, then call the external API, then record the external result in a new transaction.

## Transaction Boundaries

- One transaction per write operation is the default.
- Never hold a transaction open across an external API call or await.
- If a handler needs multiple writes, each gets its own transaction with its own idempotency guarantee.
- Use SQLAlchemy's `session.begin()` context manager for explicit boundaries:

```python
async with session.begin():
    # All writes here commit or rollback atomically
    ...
```

## Database-Level Deduplication

Use instead of application-level replay/memo when:
- The operation is a single write: unique constraint + SELECT-first in SERIALIZABLE.
- The operation spans steps: idempotency key column per step, checked before execution.
- The operation calls an external API: pass an idempotency key to the provider.

Patterns:
- **Unique constraint**: SELECT by key first, INSERT if absent, inside SERIALIZABLE. See [database.md](database.md) for query patterns.
- **Status column**: `WHERE status = 'pending'` guards prevent re-execution of completed steps.
- **Idempotency table**: a dedicated `idempotency_keys` table mapping `(key) -> (response, created_at)` for caching responses across retries.

## Ordering Guarantees

- Within a single SERIALIZABLE transaction, all reads see a consistent snapshot and all writes are atomic.
- Across transactions, ordering is not guaranteed unless enforced explicitly.
- Use `SELECT ... FOR UPDATE` to serialize concurrent access to the same row across transactions.
- Use PostgreSQL advisory locks for cross-request serialization on a logical resource.
- For strict ordering of writes to the same entity, use an optimistic concurrency `version` column:

```python
stmt = (
    update(Order)
    .where(Order.id == order_id, Order.version == expected_version)
    .values(status="shipped", version=expected_version + 1)
)
result = await session.execute(stmt)
if result.rowcount == 0:
    raise ConcurrentModificationError()
```

## Choosing an Approach

- One DB transaction owns the mutation -> single write with SERIALIZABLE isolation.
- Read-only -> read-only transaction or no transaction.
- One external API call -> commit DB state first, call API, record result in new transaction. Use idempotency keys.
- Multiple independent side effects -> multi-step with per-step idempotency, or durable workflow for autonomous completion.
- Need serialization on a shared resource -> `SELECT FOR UPDATE` or advisory locks.
- Check-then-act pattern -> `SELECT FOR UPDATE` within SERIALIZABLE, or optimistic concurrency with version column.
