from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import threading
import time

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Index, inspect, text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.schema import ColumnCollectionConstraint, Table

from .persistence import Base


_SQL_STRING_LITERAL_RE = re.compile(r"'((?:''|[^'])*)'")


@dataclass(frozen=True, slots=True)
class ReflectedCheckConstraint:
    name: str
    sqltext: str


@dataclass(frozen=True, slots=True)
class ReflectedForeignKey:
    constrained_columns: tuple[str, ...]
    referred_table: str | None
    ondelete: str


@dataclass(frozen=True, slots=True)
class ReflectedIndex:
    name: str
    unique: bool
    column_names: tuple[str, ...]
    dialect_options_text: str


def _model_tables() -> tuple[Table, ...]:
    return tuple(sorted(Base.metadata.tables.values(), key=lambda table: table.name))


def _required_table_names() -> tuple[str, ...]:
    return ("alembic_version", *(table.name for table in _model_tables()))


def _constraint_columns(constraint: ColumnCollectionConstraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def _sql_fragment(value: object) -> str:
    compile_method = getattr(value, "compile", None)
    if callable(compile_method):
        return str(
            compile_method(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
    return str(value)


def _sql_string_literals(sql_text: str) -> frozenset[str]:
    return frozenset(
        match.group(1).replace("''", "'") for match in _SQL_STRING_LITERAL_RE.finditer(sql_text)
    )


def _contains_identifier(sql_text: str, identifier: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])", sql_text)
        is not None
    )


def _is_generated_not_null_constraint(name: str) -> bool:
    return re.fullmatch(r"\d+_\d+_\d+_not_null", name) is not None


def _index_column_names(index: Index) -> tuple[str, ...]:
    column_names: list[str] = []
    for expression in index.expressions:
        column_name = getattr(expression, "name", None)
        if isinstance(column_name, str):
            column_names.append(column_name)
    return tuple(column_names)


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _reflected_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _reflected_check_constraint(value: object) -> ReflectedCheckConstraint | None:
    mapping = _reflected_mapping(value)
    if mapping is None:
        return None
    name = mapping.get("name")
    if not isinstance(name, str) or _is_generated_not_null_constraint(name):
        return None
    return ReflectedCheckConstraint(
        name=name,
        sqltext=str(mapping.get("sqltext") or ""),
    )


def _reflected_foreign_key(value: object) -> ReflectedForeignKey | None:
    mapping = _reflected_mapping(value)
    if mapping is None:
        return None
    constrained_columns = _string_sequence(mapping.get("constrained_columns"))
    if not constrained_columns:
        return None
    referred_table = mapping.get("referred_table")
    options = mapping.get("options")
    return ReflectedForeignKey(
        constrained_columns=constrained_columns,
        referred_table=referred_table if isinstance(referred_table, str) else None,
        ondelete=_ondelete(options.get("ondelete")) if isinstance(options, Mapping) else "",
    )


def _reflected_index(value: object) -> ReflectedIndex | None:
    mapping = _reflected_mapping(value)
    if mapping is None:
        return None
    name = mapping.get("name")
    if not isinstance(name, str):
        return None
    return ReflectedIndex(
        name=name,
        unique=mapping.get("unique") is True,
        column_names=_string_sequence(mapping.get("column_names")),
        dialect_options_text=str(mapping.get("dialect_options") or ""),
    )


def _ondelete(option: object) -> str:
    return str(option).upper() if option is not None else ""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    project_root = _project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations(database_url: str, *, revision: str = "head") -> None:
    command.upgrade(_alembic_config(database_url), revision)


def schema_readiness_issues(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    issues = [
        f"missing_table:{table_name}"
        for table_name in _required_table_names()
        if not inspector.has_table(table_name)
    ]
    if issues:
        return issues

    with engine.connect() as connection:
        current_revision = connection.execute(text("SELECT version_num FROM alembic_version"))
        current_revisions = {str(row[0]) for row in current_revision}
    heads = set(ScriptDirectory.from_config(_alembic_config(str(engine.url))).get_heads())
    for head in sorted(heads - current_revisions):
        issues.append(f"missing_alembic_head:{head}")

    for table in _model_tables():
        table_name = table.name
        column_names = tuple(column.name for column in table.columns)
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            if column_name not in existing_columns:
                issues.append(f"missing_column:{table_name}.{column_name}")
        for column_name in sorted(existing_columns - set(column_names)):
            issues.append(f"unexpected_column:{table_name}.{column_name}")

        primary_key_columns = tuple(column.name for column in table.primary_key.columns)
        reflected_primary_key = inspector.get_pk_constraint(table_name)
        existing_primary_key = tuple(
            str(column_name)
            for column_name in reflected_primary_key.get("constrained_columns") or ()
        )
        if existing_primary_key != primary_key_columns:
            issues.append(f"wrong_primary_key:{table_name}")

        existing_unique_constraints = {
            str(constraint["name"]): tuple(
                str(column_name) for column_name in constraint.get("column_names") or ()
            )
            for constraint in inspector.get_unique_constraints(table_name)
            if isinstance(constraint.get("name"), str)
        }
        for unique_constraint in table.constraints:
            if not isinstance(unique_constraint, UniqueConstraint):
                continue
            constraint_name = unique_constraint.name
            expected_columns = _constraint_columns(unique_constraint)
            if not isinstance(constraint_name, str):
                if expected_columns not in existing_unique_constraints.values():
                    issues.append(
                        f"missing_unique_constraint:{table_name}.{','.join(expected_columns)}"
                    )
                continue
            actual_columns = existing_unique_constraints.get(constraint_name)
            if actual_columns is None:
                issues.append(f"missing_unique_constraint:{table_name}.{constraint_name}")
                continue
            if actual_columns != expected_columns:
                issues.append(f"wrong_unique_constraint:{table_name}.{constraint_name}")

        existing_constraints: dict[str, ReflectedCheckConstraint] = {}
        for constraint in inspector.get_check_constraints(table_name):
            reflected_constraint = _reflected_check_constraint(constraint)
            if reflected_constraint is None:
                continue
            existing_constraints[reflected_constraint.name] = reflected_constraint
        expected_constraint_names: set[str] = set()
        for check_constraint in table.constraints:
            if isinstance(check_constraint, CheckConstraint) and isinstance(
                check_constraint.name, str
            ):
                expected_constraint_names.add(check_constraint.name)
        for constraint_name in sorted(existing_constraints.keys() - expected_constraint_names):
            issues.append(f"unexpected_constraint:{table_name}.{constraint_name}")
        for check_constraint in table.constraints:
            if not isinstance(check_constraint, CheckConstraint):
                continue
            constraint_name = check_constraint.name
            if not isinstance(constraint_name, str):
                continue
            if constraint_name not in existing_constraints:
                issues.append(f"missing_constraint:{table_name}.{constraint_name}")
                continue
            reflected_constraint = existing_constraints[constraint_name]
            expected_sql_text = _sql_fragment(check_constraint.sqltext)
            expected_literals = _sql_string_literals(expected_sql_text)
            actual_literals = _sql_string_literals(reflected_constraint.sqltext)
            expected_column_fragments = tuple(
                column.name
                for column in table.columns
                if _contains_identifier(expected_sql_text, column.name)
            )
            missing_literal = not expected_literals.issubset(actual_literals)
            missing_column = any(
                not _contains_identifier(reflected_constraint.sqltext, column_name)
                for column_name in expected_column_fragments
            )
            if missing_literal or missing_column:
                issues.append(f"missing_constraint_fragment:{table_name}.{constraint_name}")
            if actual_literals - expected_literals:
                issues.append(f"forbidden_constraint_fragment:{table_name}.{constraint_name}")

        existing_foreign_keys: dict[tuple[str, ...], ReflectedForeignKey] = {}
        for reflected_foreign_key in inspector.get_foreign_keys(table_name):
            foreign_key_record = _reflected_foreign_key(reflected_foreign_key)
            if foreign_key_record is None:
                continue
            existing_foreign_keys[foreign_key_record.constrained_columns] = foreign_key_record
        for foreign_key in table.foreign_key_constraints:
            expected_foreign_key_columns = _constraint_columns(foreign_key)
            column_label = (
                expected_foreign_key_columns[0]
                if len(expected_foreign_key_columns) == 1
                else ",".join(expected_foreign_key_columns)
            )
            existing_foreign_key = existing_foreign_keys.get(expected_foreign_key_columns)
            if existing_foreign_key is None:
                issues.append(f"missing_foreign_key:{table_name}.{column_label}")
                continue
            referred_tables = {element.column.table.name for element in foreign_key.elements}
            if len(referred_tables) == 1:
                expected_table = next(iter(referred_tables))
                if existing_foreign_key.referred_table != expected_table:
                    issues.append(f"wrong_foreign_key_table:{table_name}.{column_label}")
            expected_ondelete = _ondelete(foreign_key.ondelete)
            if expected_ondelete and existing_foreign_key.ondelete != expected_ondelete:
                issues.append(f"wrong_foreign_key_ondelete:{table_name}.{column_label}")

        existing_indexes: dict[str, ReflectedIndex] = {}
        for reflected_index in inspector.get_indexes(table_name):
            index_record = _reflected_index(reflected_index)
            if index_record is not None:
                existing_indexes[index_record.name] = index_record
        for index in table.indexes:
            index_name = index.name
            if index_name is None:
                continue
            if index_name not in existing_indexes:
                issues.append(f"missing_index:{table_name}.{index_name}")
                continue
            if index.unique:
                if not existing_indexes[index_name].unique:
                    issues.append(f"missing_unique_index:{table_name}.{index_name}")
            expected_index_columns = _index_column_names(index)
            if existing_indexes[index_name].column_names != expected_index_columns:
                issues.append(f"missing_index_columns:{table_name}.{index_name}")
            expected_where = index.dialect_options["postgresql"].get("where")
            if expected_where is not None:
                dialect_text = existing_indexes[index_name].dialect_options_text
                expected_where_text = _sql_fragment(expected_where)
                expected_where_literals = _sql_string_literals(expected_where_text)
                actual_where_literals = _sql_string_literals(dialect_text)
                expected_column_fragments = tuple(
                    column_name
                    for column_name in expected_index_columns
                    if _contains_identifier(expected_where_text, column_name)
                )
                missing_literal = not expected_where_literals.issubset(actual_where_literals)
                missing_column = any(
                    not _contains_identifier(dialect_text, column_name)
                    for column_name in expected_column_fragments
                )
                if missing_literal or missing_column:
                    issues.append(f"missing_index_fragment:{table_name}.{index_name}")

    return issues


class SchemaReadinessProbe:
    """TTL-cached schema readiness check shared by /v1/health and every protected handler.

    Re-runs `schema_readiness_issues(engine)` at most once per `ttl_seconds` so a
    burst of health checks does not slam the DB inspector, while a post-startup
    schema repair becomes visible within the TTL window. Lock-coalesces concurrent
    re-checks so only one thread reflects against the DB at a time."""

    def __init__(
        self,
        engine: Engine,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._engine = engine
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._cached_issues: list[str] | None = None
        self._cached_at: float | None = None

    def schema_issues(self) -> list[str]:
        now = self._clock()
        cached_issues = self._cached_issues
        cached_at = self._cached_at
        if (
            cached_issues is not None
            and cached_at is not None
            and (now - cached_at) < self._ttl_seconds
        ):
            return list(cached_issues)
        with self._lock:
            now = self._clock()
            cached_issues = self._cached_issues
            cached_at = self._cached_at
            if (
                cached_issues is not None
                and cached_at is not None
                and (now - cached_at) < self._ttl_seconds
            ):
                return list(cached_issues)
            fresh = schema_readiness_issues(self._engine)
            self._cached_issues = list(fresh)
            self._cached_at = self._clock()
            return list(fresh)

    def invalidate(self) -> None:
        with self._lock:
            self._cached_issues = None
            self._cached_at = None
