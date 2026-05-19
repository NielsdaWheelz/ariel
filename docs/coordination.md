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

`background_tasks` is the one durable task queue for work that must survive
process restarts. A single-threaded worker drains it. Because there is exactly
one worker, the queue carries no claim protocol: a row existing and due is the
only pending state.

```sql
CREATE TABLE background_tasks (
    id                        TEXT PRIMARY KEY,  -- prefixed ULID
    task_type                 TEXT NOT NULL,
    idempotency_key           TEXT,
    provider_write_receipt_id TEXT,
    payload                   JSONB NOT NULL,
    attempts                  INT NOT NULL DEFAULT 0,
    recurrence_seconds        INT,
    run_after                 TIMESTAMPTZ NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL,
    updated_at                TIMESTAMPTZ NOT NULL
);
```

- **Enqueue**: insert a row. `run_after` is when it becomes due; `task_type` is
  the discriminator the worker dispatches on. Current task types:
  `agent_wake`, `user_message`, `research_run`, `execute_action_attempt`,
  `expire_approvals`, `provider_event_received`, `provider_sync_due`,
  `provider_write_reconcile_due`, `provider_watch_renew_due`,
  `provider_reconcile_sync_due`, `agency_event_received`, `memory_remember`,
  `memory_sweep`.
- **Dequeue**: select the earliest due row ordered by `(run_after, created_at,
  id)`. No `SELECT ... FOR UPDATE SKIP LOCKED` — there is no second worker to
  race.
- **Success**: a one-shot row is deleted; a recurring row (`recurrence_seconds`
  set) is re-armed in place to `now + recurrence_seconds` with `attempts` reset.
- **Failure**: the row is left in place with `attempts` incremented and
  `run_after` pushed out for backoff.

```python
task = db.scalar(
    select(BackgroundTask)
    .where(BackgroundTask.run_after <= now)
    .order_by(
        BackgroundTask.run_after.asc(),
        BackgroundTask.created_at.asc(),
        BackgroundTask.id.asc(),
    )
    .limit(1)
)
```

There is no `status` column, no claim, no `dead_letter` state, and no row
lifecycle beyond "exists" and "deleted". A row is deleted only on success, so a
crash mid-task leaves the row to be retried on the next pass.

### Retry

- A failed task increments `attempts` and is retried after exponential backoff:
  `run_after = now + min(300, 2 ** (attempts - 1))` seconds.
- `attempts` is capped (currently 5). On exhaustion a one-shot task is abandoned
  (the row is deleted) and a recurring task is re-armed to its next occurrence.
- There is no dead-letter table. An effect that must not repeat carries an
  idempotency key in the capability layer, not a queue-level lifecycle.

### Liveness

There is no heartbeat column and no reaper. With a single-threaded worker and
no claim protocol, a task cannot be "stuck running" in another process: the
worker either completes a task and removes or re-arms its row, or crashes and
leaves the row due for the next pass. Liveness is the worker process being up,
supervised by systemd.
