from __future__ import annotations

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from ariel.db_errors import (
    is_retryable_dbapi_failure,
    is_serialization_failure,
    is_unique_constraint_failure,
)


class _DbDiag:
    def __init__(self, *, constraint_name: str | None = None) -> None:
        self.constraint_name = constraint_name


class _DbOrig(Exception):
    def __init__(
        self,
        *,
        sqlstate: str | None = None,
        pgcode: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__(sqlstate or pgcode or "db error")
        self.sqlstate = sqlstate
        self.pgcode = pgcode
        self.diag = _DbDiag(constraint_name=constraint_name)


def test_unique_constraint_failure_requires_exact_constraint() -> None:
    exc = IntegrityError(
        "INSERT",
        {},
        _DbOrig(sqlstate="23505", constraint_name="uq_example"),
    )

    assert is_unique_constraint_failure(exc, "uq_example")
    assert not is_unique_constraint_failure(exc, "uq_other")


def test_retryable_dbapi_failure_covers_postgres_retry_states_and_connection_class() -> None:
    assert is_serialization_failure(OperationalError("SELECT 1", {}, _DbOrig(sqlstate="40001")))
    assert is_retryable_dbapi_failure(OperationalError("SELECT 1", {}, _DbOrig(pgcode="57P03")))
    assert is_retryable_dbapi_failure(OperationalError("SELECT 1", {}, _DbOrig(sqlstate="40P01")))
    assert is_retryable_dbapi_failure(OperationalError("SELECT 1", {}, _DbOrig(sqlstate="08006")))


def test_operational_error_without_sqlstate_is_retryable_but_plain_dbapi_error_is_not() -> None:
    assert is_retryable_dbapi_failure(OperationalError("SELECT 1", {}, _DbOrig()))
    assert not is_retryable_dbapi_failure(DBAPIError("SELECT 1", {}, _DbOrig()))
