from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ariel.persistence import WeatherDefaultLocationRecord

_WEATHER_DEFAULT_LOCATION_ROW_ID = "weather_default_location"
_WEATHER_DEFAULT_LOCATION_ENV = "ARIEL_WEATHER_DEFAULT_LOCATION"
_MAX_LOCATION_LENGTH = 200

WeatherDefaultLocationSource = Literal["unset", "bootstrap", "user"]
WeatherLocationResolutionSource = Literal["explicit", "default", "unresolved"]


@dataclass(frozen=True, slots=True)
class WeatherDefaultLocationState:
    location: str | None
    source: WeatherDefaultLocationSource
    updated_at: datetime | None


def _normalize_location(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_LOCATION_LENGTH:
        return None
    return normalized


def _sanitize_source(value: str) -> Literal["bootstrap", "user"]:
    return "bootstrap" if value == "bootstrap" else "user"


def _load_weather_default_location_record(*, db: Session) -> WeatherDefaultLocationRecord | None:
    return db.scalar(
        select(WeatherDefaultLocationRecord)
        .where(WeatherDefaultLocationRecord.id == _WEATHER_DEFAULT_LOCATION_ROW_ID)
        .limit(1)
    )


def _state_from_record(record: WeatherDefaultLocationRecord) -> WeatherDefaultLocationState:
    return WeatherDefaultLocationState(
        location=record.default_location,
        source=_sanitize_source(record.source),
        updated_at=record.updated_at,
    )


def _try_insert_bootstrap_location(
    *,
    db: Session,
    location: str,
    now: datetime,
) -> WeatherDefaultLocationRecord | None:
    with db.begin_nested():
        record = WeatherDefaultLocationRecord(
            id=_WEATHER_DEFAULT_LOCATION_ROW_ID,
            default_location=location,
            source="bootstrap",
            created_at=now,
            updated_at=now,
        )
        db.add(record)
        try:
            db.flush()
            return record
        except IntegrityError:
            return None


def _load_weather_default_location_record_for_update(
    *,
    db: Session,
) -> WeatherDefaultLocationRecord | None:
    return db.scalar(
        select(WeatherDefaultLocationRecord)
        .where(WeatherDefaultLocationRecord.id == _WEATHER_DEFAULT_LOCATION_ROW_ID)
        .with_for_update()
        .limit(1)
    )


def get_weather_default_location_state(
    *,
    db: Session,
    now_fn: Callable[[], datetime],
    bootstrap_if_unset: bool,
) -> WeatherDefaultLocationState:
    record = _load_weather_default_location_record(db=db)
    if record is not None:
        return _state_from_record(record)

    if not bootstrap_if_unset:
        return WeatherDefaultLocationState(location=None, source="unset", updated_at=None)

    bootstrap_location = _normalize_location(os.getenv(_WEATHER_DEFAULT_LOCATION_ENV))
    if bootstrap_location is None:
        return WeatherDefaultLocationState(location=None, source="unset", updated_at=None)

    inserted_record = _try_insert_bootstrap_location(
        db=db,
        location=bootstrap_location,
        now=now_fn(),
    )
    if inserted_record is not None:
        return _state_from_record(inserted_record)

    # Another transaction may have initialized canonical state first.
    latest_record = _load_weather_default_location_record(db=db)
    if latest_record is not None:
        return _state_from_record(latest_record)
    return WeatherDefaultLocationState(location=None, source="unset", updated_at=None)


def set_weather_default_location(
    *,
    db: Session,
    location: str,
    now_fn: Callable[[], datetime],
) -> WeatherDefaultLocationState:
    normalized_location = _normalize_location(location)
    if normalized_location is None:
        msg = "weather default location must be non-empty and <= 200 characters"
        raise ValueError(msg)

    record = _load_weather_default_location_record_for_update(db=db)
    now = now_fn()
    if record is None:
        with db.begin_nested():
            candidate = WeatherDefaultLocationRecord(
                id=_WEATHER_DEFAULT_LOCATION_ROW_ID,
                default_location=normalized_location,
                source="user",
                created_at=now,
                updated_at=now,
            )
            db.add(candidate)
            try:
                db.flush()
                record = candidate
            except IntegrityError:
                record = None
        if record is None:
            record = _load_weather_default_location_record_for_update(db=db)
    if record is None:
        msg = "failed to initialize canonical weather default location state"
        raise RuntimeError(msg)
    record.default_location = normalized_location
    record.source = "user"
    record.updated_at = now
    db.flush()
    return _state_from_record(record)


def resolve_weather_location(
    *,
    db: Session,
    explicit_location: str | None,
    now_fn: Callable[[], datetime],
) -> tuple[str | None, WeatherLocationResolutionSource]:
    normalized_explicit = _normalize_location(explicit_location)
    if normalized_explicit is not None:
        return normalized_explicit, "explicit"

    default_state = get_weather_default_location_state(
        db=db,
        now_fn=now_fn,
        bootstrap_if_unset=True,
    )
    if default_state.location is not None:
        return default_state.location, "default"
    return None, "unresolved"
