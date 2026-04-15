# Database

## Scope

PostgreSQL 16 schema rules, SQLAlchemy 2.0 query patterns, transaction boundaries, and DB-specific conventions.

## Schema

- Every table has a primary key.
- Primary keys are prefixed ULIDs: `prefix_ulid` (e.g., `ses_01j5...`). Generated via `ulid.new()`.
- Column type for IDs: `String(32)`.
- Every table includes `created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)`.
- Tables that track mutation include `updated_at` with the same type.
- Use `JSONB` for structured data (never `Text` for JSON payloads). Default: `mapped_column(JSONB, nullable=False, default=dict)`.
- Models inherit from a shared `DeclarativeBase` subclass.
- All modules use `from __future__ import annotations`.
- Alembic manages migrations. Schema and migrations live together.

## Foreign Keys

- Default: `ondelete="RESTRICT"`.
- Do not use `ON DELETE CASCADE` or other database-level cascading operations.
- Cleanup is explicit in application code.

## Timestamps

- Use `DateTime(timezone=True)` (maps to `timestamptz`). Never `DateTime()` without timezone.
- Compare against DB-stored timestamps with `func.now()` or `text('now()')`, not the local clock.
- The database is the authoritative clock shared across all app servers.
- Serialize to RFC 3339 with `datetime.astimezone(UTC).isoformat()`.

## Time Intervals

- Time intervals are right-open: `[start, end)`.
- `expires_at` is the first moment of invalidity: active while `now < expires_at`, expired once `now >= expires_at`.

## Indexes

- Do not add indexes speculatively. Add them when a query pattern on a high-volume table needs one.
- Use database uniqueness for true schema-owned keys: primary keys and real local alternate keys.
- Do not use unique constraints to encode application-level ownership invariants.
- Partial unique indexes via `postgresql_where=` are fine for natural constraints (e.g., single active session).

## Check Constraints

- Enumerate allowed values with `CheckConstraint` in `__table_args__`, not application-only validation.
- Name every constraint: `ck_<table>_<description>`.
- Use compound checks to enforce field-group consistency (e.g., if state A then field X must be non-null).

## Query Patterns

- Do not use `INSERT ... ON CONFLICT`.
- Use an explicit SELECT to check for an existing row, then INSERT, UPDATE, or DELETE accordingly.
- This is safe inside SERIALIZABLE transactions -- concurrent conflicts cause a serialization failure that triggers a retry.
- The same applies to DELETE. Without an existence check, concurrent deletes can both report success.
- Assert that row counts match expectations after a mutation as a defect catcher.
- Use `db.scalar(select(...))` for single-row reads. Returns `None` when not found.
- Use `db.scalars(select(...)).all()` for multi-row reads.
- Use `db.add()` then `db.flush()` for inserts. Flush inside the transaction to surface `IntegrityError` early.
- Catch `IntegrityError` explicitly for expected races (e.g., unique constraint on concurrent create), then re-query.

## Transaction Boundaries

Session factory setup:

```python
engine = create_engine(db_url, future=True, pool_pre_ping=True)
session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
```

All DB work uses sync `Session` via `sessionmaker`. Transaction scopes:

```python
# Read or write -- explicit begin/commit via context manager
with session_factory() as db:
    with db.begin():
        row = db.scalar(select(Foo).where(...))
        db.add(Bar(...))
        db.flush()
```

- `with db.begin()` opens a SERIALIZABLE transaction and commits on clean exit, rolls back on exception.
- Use `db.begin_nested()` (savepoint) for speculative inserts that may hit `IntegrityError`.
- Never nest `db.begin()` calls. Use `begin_nested()` for sub-transaction semantics.
- Do not run non-DB side effects inside a DB transaction; they cannot be rolled back on serialization retry.

## Advisory Locks

Use `pg_advisory_xact_lock` for cross-session coordination within a transaction:

```python
db.execute(
    text("SELECT pg_advisory_xact_lock(:lock_id)"),
    {"lock_id": lock_id},
)
```

Lock IDs are derived from a stable hash of the resource name. The lock is released when the transaction ends.

## Idempotency

- Idempotency keys are nullable `String(128)` columns with partial unique indexes:

```python
Index(
    "ix_<table>_idempotency_key_unique",
    "idempotency_key",
    unique=True,
    postgresql_where=(idempotency_key.is_not(None)),
)
```

- On a write path: SELECT by idempotency key first. If found, return the cached result. If not, perform the write and persist the key atomically in the same transaction.

## Model Conventions

- ORM models use `Mapped[]` type annotations with `mapped_column()`.
- Relationships use `Mapped[list["Foo"]]` with `relationship(back_populates=...)`.
- Table args (constraints, indexes) go in `__table_args__` as a tuple.
- String enum columns use `String(32)` with a `CheckConstraint`, not SQLAlchemy `Enum`.

## Further Reading

- See [concurrency.md](concurrency.md) for linearization rules.
- See [mutation-ordering.md](mutation-ordering.md) for ordering mutations across systems.
