# Coordination

## Scope

PostgreSQL-native coordination patterns for multi-process safety, idempotency, and background task management.

## PostgreSQL-Native Coordination

### Advisory Locks

Use for cross-request serialization on logical resources.

- **Transaction-scoped** (`pg_advisory_xact_lock`): auto-released on commit/rollback. Default choice.
- **Session-scoped** (`pg_advisory_lock`): survives transaction boundaries. Use for multi-step workflows.
- **Try variants** (`pg_try_advisory_lock`): non-blocking. Use when you want to skip or fail fast rather than wait.

Lock keys are `bigint`. Hash string identifiers to integers:

```python
import hashlib

def advisory_lock_key(namespace: str, resource_id: str) -> int:
    h = hashlib.sha256(f"{namespace}:{resource_id}".encode()).digest()
    return int.from_bytes(h[:8], "big", signed=True)
```

### LISTEN / NOTIFY

Use for real-time signaling between processes. Not for data transfer.

- `NOTIFY channel, payload` to signal.
- `LISTEN channel` to subscribe.
- Payload limit: 8000 bytes. Send IDs, not data.
- Notifications are lost if no listener is active. Always pair with polling as a fallback.

```python
# Publisher (inside transaction)
await session.execute(text("NOTIFY order_updates, :id"), {"id": str(order_id)})

# Subscriber (separate connection, not inside a transaction)
async for notification in listener:
    await process_order(notification.payload)
```

### SERIALIZABLE Isolation

- Default isolation for write transactions.
- PostgreSQL aborts conflicting transactions with serialization failures (`40001`).
- Retry serialization failures at the request level (middleware or decorator).

## Idempotency

### Unique Constraints + SELECT-First

The primary idempotency mechanism. Every mutation that can be retried must have a deduplication key.

Inside a SERIALIZABLE transaction, SELECT by key first. If found, return the cached result. If not, INSERT:

```python
async with session.begin():
    existing = await session.scalar(
        select(Operation).where(Operation.idempotency_key == key)
    )
    if existing:
        return existing

    op = Operation(idempotency_key=key, result=result_json)
    session.add(op)
    await session.flush()
    return op
```

Use `begin_nested()` if catching `IntegrityError` from a concurrent race without aborting the outer transaction.

### Idempotency Key Table

For operations where you need to cache the full response:

```sql
CREATE TABLE idempotency_keys (
    key         TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,
    response    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '72 hours'
);
```

- Check before executing. Return cached response on hit.
- Insert atomically with the mutation (same transaction).
- Prune expired rows on a schedule.

### Status Column

For multi-step operations, use a status column to track progress:

```python
# Only execute step if not already done
account = await session.get(Account, account_id)
if account.provision_status == "pending":
    await external_api.provision(account_id)
    account.provision_status = "provisioned"
```

## Background Tasks

### Task Queue Pattern

Use a PostgreSQL-backed task queue (or arq/Celery) for work that must survive process restarts.

- Enqueue: insert a row into a tasks table.
- Dequeue: `SELECT ... FOR UPDATE SKIP LOCKED` to claim work without blocking.
- Completion: update status to `completed` and commit.
- Failure: update status to `failed` with error details.

```sql
CREATE TABLE background_tasks (
    id          TEXT PRIMARY KEY,  -- prefixed ULID
    task_type   TEXT NOT NULL,
    payload     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 3,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_after   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Claim work:

```python
async with session.begin():
    task = (await session.execute(
        select(BackgroundTask)
        .where(BackgroundTask.status == "pending", BackgroundTask.run_after <= func.now())
        .order_by(BackgroundTask.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )).scalar_one_or_none()

    if task:
        task.status = "running"
        task.attempts += 1
```

## Dead Letter / Retry

- Tasks that exceed `max_retries` move to `dead_letter` status.
- Dead letters are operational containment and debugging, not application control flow.
- Log the error, payload, and attempt history for diagnosis.
- Dead letters can be retried manually (update status back to `pending`).
- Use exponential backoff for retries: `run_after = now() + interval * 2^attempts`.
- Prune completed and expired dead-letter rows on a schedule.

## Liveness Detection

For long-running tasks, use a heartbeat column:

- The worker updates `last_heartbeat` periodically.
- A reaper process marks tasks as `failed` if `last_heartbeat` exceeds a threshold.
- This prevents stuck tasks from blocking the queue.

```python
# Worker heartbeat
task.last_heartbeat = func.now()
await session.commit()

# Reaper query
await session.execute(
    update(BackgroundTask)
    .where(
        BackgroundTask.status == "running",
        BackgroundTask.last_heartbeat < func.now() - text("INTERVAL '5 minutes'"),
    )
    .values(status="pending", error="heartbeat timeout")
)
```
