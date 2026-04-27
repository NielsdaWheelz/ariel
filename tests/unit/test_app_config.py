from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from ariel.app import create_app
from ariel.config import AppSettings


def _app_settings_without_env_files() -> AppSettings:
    return cast(Any, AppSettings)(_env_file=None)


def test_app_settings_load_from_project_env_files() -> None:
    env_files = AppSettings.model_config.get("env_file")
    assert env_files is not None
    if isinstance(env_files, (str, Path)):
        env_paths = [Path(env_files)]
    else:
        env_paths = [Path(path) for path in env_files]
    env_file_names = {path.name for path in env_paths}
    assert ".env.local" in env_file_names
    assert ".env" in env_file_names


def test_create_app_uses_ariel_database_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_DATABASE_URL", "postgresql+psycopg://env-user:env-pass@localhost/env-db")

    app = create_app()
    try:
        assert str(app.state.engine.url) == "postgresql+psycopg://env-user:***@localhost/env-db"
    finally:
        app.state.engine.dispose()


def test_explicit_database_url_takes_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ARIEL_DATABASE_URL",
        "postgresql+psycopg://env-user:env-pass@localhost/env-db",
    )

    app = create_app(database_url="postgresql+psycopg://arg-user:arg-pass@localhost/arg-db")
    try:
        assert str(app.state.engine.url) == "postgresql+psycopg://arg-user:***@localhost/arg-db"
    finally:
        app.state.engine.dispose()


def test_bind_host_rejects_public_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_BIND_HOST", "0.0.0.0")

    with pytest.raises(ValidationError):
        AppSettings()


def test_max_recent_turns_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "7")

    settings = AppSettings()
    assert settings.max_recent_turns == 7


def test_max_recent_turns_rejects_non_positive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECENT_TURNS", "0")

    with pytest.raises(ValidationError):
        AppSettings()


def test_slice1_turn_budget_defaults_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIEL_MAX_RECENT_TURNS", raising=False)
    monkeypatch.delenv("ARIEL_MAX_RECALLED_MEMORIES", raising=False)
    monkeypatch.delenv("ARIEL_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("ARIEL_AUTO_ROTATE_MAX_TURNS", raising=False)
    monkeypatch.delenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", raising=False)
    monkeypatch.delenv("ARIEL_MAX_RESPONSE_TOKENS", raising=False)
    monkeypatch.delenv("ARIEL_MAX_MODEL_ATTEMPTS", raising=False)
    monkeypatch.delenv("ARIEL_MAX_TURN_WALL_TIME_MS", raising=False)
    monkeypatch.delenv("ARIEL_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_APPROVAL_ACTOR_ID", raising=False)

    settings = AppSettings.model_validate({})
    assert settings.max_recent_turns == 12
    assert settings.max_recalled_memories == 8
    assert settings.max_context_tokens == 6000
    assert settings.auto_rotate_max_turns == 120
    assert settings.auto_rotate_max_age_seconds == 172800
    assert settings.auto_rotate_context_pressure_tokens == 5400
    assert settings.max_response_tokens == 700
    assert settings.max_model_attempts == 2
    assert settings.max_turn_wall_time_ms == 20000
    assert settings.approval_ttl_seconds == 900
    assert settings.approval_actor_id == "user.local"


def test_turn_budget_env_overrides_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECALLED_MEMORIES", "11")
    monkeypatch.setenv("ARIEL_MAX_CONTEXT_TOKENS", "4321")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "77")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "2222")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "3333")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "321")
    monkeypatch.setenv("ARIEL_MAX_MODEL_ATTEMPTS", "4")
    monkeypatch.setenv("ARIEL_MAX_TURN_WALL_TIME_MS", "15000")
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "1200")
    monkeypatch.setenv("ARIEL_APPROVAL_ACTOR_ID", "user.integration")

    settings = AppSettings()
    assert settings.max_recalled_memories == 11
    assert settings.max_context_tokens == 4321
    assert settings.auto_rotate_max_turns == 77
    assert settings.auto_rotate_max_age_seconds == 2222
    assert settings.auto_rotate_context_pressure_tokens == 3333
    assert settings.max_response_tokens == 321
    assert settings.max_model_attempts == 4
    assert settings.max_turn_wall_time_ms == 15000
    assert settings.approval_ttl_seconds == 1200
    assert settings.approval_actor_id == "user.integration"


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("ARIEL_MAX_RECALLED_MEMORIES", "0"),
        ("ARIEL_MAX_CONTEXT_TOKENS", "0"),
        ("ARIEL_AUTO_ROTATE_MAX_TURNS", "0"),
        ("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "0"),
        ("ARIEL_AUTO_ROTATE_CONTEXT_PRESSURE_TOKENS", "0"),
        ("ARIEL_MAX_RESPONSE_TOKENS", "0"),
        ("ARIEL_MAX_MODEL_ATTEMPTS", "0"),
        ("ARIEL_MAX_TURN_WALL_TIME_MS", "0"),
        ("ARIEL_APPROVAL_TTL_SECONDS", "0"),
    ],
)
def test_turn_budget_fields_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(ValidationError):
        AppSettings()


def test_approval_actor_id_rejects_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_APPROVAL_ACTOR_ID", "   ")

    with pytest.raises(ValidationError):
        AppSettings()


def test_discord_settings_default_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIEL_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ARIEL_DISCORD_GUILD_ID", raising=False)
    monkeypatch.delenv("ARIEL_DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ARIEL_DISCORD_USER_ID", raising=False)
    monkeypatch.delenv("ARIEL_DISCORD_ARIEL_BASE_URL", raising=False)

    settings = _app_settings_without_env_files()
    assert settings.discord_bot_token is None
    assert settings.discord_guild_id is None
    assert settings.discord_channel_id is None
    assert settings.discord_user_id is None
    assert settings.discord_ariel_base_url == "http://127.0.0.1:8000"
    assert settings.discord_notification_timeout_seconds == 10.0
    assert settings.agency_event_secret is None
    assert settings.agency_event_max_skew_seconds == 300
    assert settings.worker_poll_seconds == 1.0
    assert settings.worker_heartbeat_timeout_seconds == 300


def test_discord_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("ARIEL_DISCORD_GUILD_ID", "222")
    monkeypatch.setenv("ARIEL_DISCORD_CHANNEL_ID", "333")
    monkeypatch.setenv("ARIEL_DISCORD_USER_ID", "444")
    monkeypatch.setenv("ARIEL_DISCORD_ARIEL_BASE_URL", "http://127.0.0.1:9000/")
    monkeypatch.setenv("ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_SECRET", "agency-secret")
    monkeypatch.setenv("ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS", "120")
    monkeypatch.setenv("ARIEL_WORKER_POLL_SECONDS", "0.25")
    monkeypatch.setenv("ARIEL_WORKER_HEARTBEAT_TIMEOUT_SECONDS", "45")

    settings = _app_settings_without_env_files()
    assert settings.discord_bot_token == "discord-token"
    assert settings.discord_guild_id == 222
    assert settings.discord_channel_id == 333
    assert settings.discord_user_id == 444
    assert settings.discord_ariel_base_url == "http://127.0.0.1:9000"
    assert settings.discord_notification_timeout_seconds == 7.5
    assert settings.agency_event_secret == "agency-secret"
    assert settings.agency_event_max_skew_seconds == 120
    assert settings.worker_poll_seconds == 0.25
    assert settings.worker_heartbeat_timeout_seconds == 45


@pytest.mark.parametrize(
    "env_name",
    [
        "ARIEL_DISCORD_GUILD_ID",
        "ARIEL_DISCORD_CHANNEL_ID",
        "ARIEL_DISCORD_USER_ID",
    ],
)
def test_discord_ids_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


def test_discord_base_url_must_be_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_DISCORD_ARIEL_BASE_URL", "not-a-url")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


@pytest.mark.parametrize(
    "env_name",
    [
        "ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS",
        "ARIEL_WORKER_POLL_SECONDS",
        "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS",
        "ARIEL_WORKER_HEARTBEAT_TIMEOUT_SECONDS",
    ],
)
def test_worker_and_agency_numeric_settings_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()
