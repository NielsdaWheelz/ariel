from __future__ import annotations

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

RETRYABLE_DB_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53300",  # too_many_connections
        "55P03",  # lock_not_available
        "57014",  # query_canceled
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)


def _db_sqlstate(exc: DBAPIError) -> str | None:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if isinstance(sqlstate, str):
        return sqlstate
    pgcode = getattr(exc.orig, "pgcode", None)
    return pgcode if isinstance(pgcode, str) else None


def _db_constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


def is_unique_constraint_failure(exc: IntegrityError, constraint_name: str) -> bool:
    return _db_sqlstate(exc) == "23505" and _db_constraint_name(exc) == constraint_name


def is_serialization_failure(exc: DBAPIError) -> bool:
    return _db_sqlstate(exc) == "40001"


def is_retryable_dbapi_failure(exc: DBAPIError) -> bool:
    sqlstate = _db_sqlstate(exc)
    if sqlstate is None:
        return isinstance(exc, OperationalError)
    return sqlstate.startswith("08") or sqlstate in RETRYABLE_DB_SQLSTATES
