from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from ariel.app import create_app
from ariel.config import AppSettings, _ENV_FILES

STRONG_LOCAL_AUTH_TOKEN = "test_local_auth_token_0123456789abcdef"
CONNECTOR_KEYRING = '{"v1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}'


def _app_settings_without_env_files() -> AppSettings:
    return cast(Any, AppSettings)(_env_file=None)


@pytest.mark.uses_real_env_files
def test_app_settings_load_from_project_env_files() -> None:
    assert {path.name for path in _ENV_FILES} == {".env", ".env.local"}
    assert AppSettings.model_config["env_file"] == _ENV_FILES


def test_create_app_uses_ariel_database_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ARIEL_DATABASE_URL", "postgresql+psycopg://env-user:env-pass@localhost/env-db"
    )

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
    monkeypatch.delenv("ARIEL_MAX_RECALLED_ASSERTIONS", raising=False)
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
    assert settings.max_recalled_assertions == 8
    assert settings.max_context_tokens == 6000
    assert settings.auto_rotate_max_turns == 120
    assert settings.auto_rotate_max_age_seconds == 172800
    assert settings.auto_rotate_context_pressure_tokens == 5400
    assert settings.max_response_tokens == 700
    assert settings.max_model_attempts == 2
    assert settings.max_turn_wall_time_ms == 20000
    assert settings.approval_ttl_seconds == 900
    assert settings.approval_actor_id == "user.local"


def test_security_defaults_are_development_only() -> None:
    settings = _app_settings_without_env_files()
    assert settings.deployment_mode == "development"
    assert settings.local_auth_required is False
    assert settings.connector_encryption_secret == "dev-local-connector-secret"


def test_production_rejects_unauthenticated_local_api() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate(
            {
                "deployment_mode": "production",
                "local_auth_required": False,
                "local_auth_token": STRONG_LOCAL_AUTH_TOKEN,
                "connector_encryption_secret": "prod-connector-secret",
                "connector_encryption_keys": CONNECTOR_KEYRING,
            }
        )


def test_production_rejects_dev_connector_encryption_secret() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate(
            {
                "deployment_mode": "production",
                "local_auth_required": True,
                "local_auth_token": STRONG_LOCAL_AUTH_TOKEN,
                "connector_encryption_secret": "dev-local-connector-secret",
                "connector_encryption_keys": CONNECTOR_KEYRING,
            }
        )


def test_production_requires_connector_keyring() -> None:
    with pytest.raises(ValidationError):
        cast(Any, AppSettings)(
            _env_file=None,
            deployment_mode="production",
            local_auth_required=True,
            local_auth_token=STRONG_LOCAL_AUTH_TOKEN,
            connector_encryption_secret="prod-connector-secret",
        )


def test_local_auth_rejects_weak_tokens() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate(
            {
                "local_auth_required": True,
                "local_auth_token": "test-local-token",
            }
        )


def test_turn_budget_env_overrides_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MAX_RECALLED_ASSERTIONS", "11")
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
    assert settings.max_recalled_assertions == 11
    assert settings.max_context_tokens == 4321
    assert settings.auto_rotate_max_turns == 77
    assert settings.auto_rotate_max_age_seconds == 2222
    assert settings.auto_rotate_context_pressure_tokens == 3333
    assert settings.max_response_tokens == 321
    assert settings.max_model_attempts == 4
    assert settings.max_turn_wall_time_ms == 15000
    assert settings.approval_ttl_seconds == 1200
    assert settings.approval_actor_id == "user.integration"


def test_memory_runtime_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_MODEL", "fixture-embedding")
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setenv("ARIEL_MEMORY_IMPORT_CUTOVER_ENABLED", "true")

    settings = AppSettings()
    assert settings.memory_embedding_provider == "local"
    assert settings.memory_embedding_model == "fixture-embedding"
    assert settings.memory_embedding_dimensions == 1536
    assert settings.memory_import_cutover_enabled is True


def test_memory_embedding_dimensions_must_match_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_DIMENSIONS", "3072")

    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("ARIEL_MAX_RECALLED_ASSERTIONS", "0"),
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
    assert settings.terminal_output_limit_bytes == 12000
    assert settings.terminal_run_timeout_seconds == 30.0
    assert settings.terminal_background_timeout_seconds == 3600
    assert settings.terminal_timeout_kill_after_seconds == 5.0


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
    monkeypatch.setenv("ARIEL_TERMINAL_OUTPUT_LIMIT_BYTES", "4096")
    monkeypatch.setenv("ARIEL_TERMINAL_RUN_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("ARIEL_TERMINAL_BACKGROUND_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("ARIEL_TERMINAL_TIMEOUT_KILL_AFTER_SECONDS", "0.5")

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
    assert settings.terminal_output_limit_bytes == 4096
    assert settings.terminal_run_timeout_seconds == 2.5
    assert settings.terminal_background_timeout_seconds == 120
    assert settings.terminal_timeout_kill_after_seconds == 0.5


def test_provider_runtime_settings_default_to_production_values() -> None:
    settings = _app_settings_without_env_files()

    assert settings.terminal_dir == "~/.cache/ariel/terminal"
    assert settings.search_brave_base_url == "https://api.search.brave.com/res/v1"
    assert settings.search_web_timeout_seconds == 8.0
    assert settings.search_web_api_key is None
    assert settings.search_news_timeout_seconds == 8.0
    assert settings.search_news_api_key is None
    assert settings.web_extract_provider_endpoint is None
    assert settings.web_extract_timeout_seconds == 10.0
    assert settings.web_extract_max_retries == 2
    assert settings.web_extract_api_key is None
    assert settings.maps_provider_endpoint is None
    assert settings.maps_provider_timeout_seconds == 8.0
    assert settings.maps_provider_api_key_enc is None
    assert settings.weather_provider_mode == "production"
    assert settings.weather_production_endpoint == "https://api.tomorrow.io/v4/weather/forecast"
    assert settings.weather_production_timeout_seconds == 8.0
    assert settings.weather_production_api_key is None
    assert settings.weather_dev_endpoint == "https://wttr.in"
    assert settings.weather_dev_timeout_seconds == 8.0
    assert settings.weather_default_location is None


def test_provider_runtime_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_TERMINAL_DIR", "/tmp/ariel-terminal")
    monkeypatch.setenv("ARIEL_SEARCH_BRAVE_BASE_URL", "https://search.example.test/res/v1")
    monkeypatch.setenv("ARIEL_SEARCH_WEB_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "search-key")
    monkeypatch.setenv("ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("ARIEL_SEARCH_NEWS_API_KEY", "news-key")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT", "https://extract.example.test")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_MAX_RETRIES", "4")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_API_KEY", "extract-key")
    monkeypatch.setenv("ARIEL_MAPS_PROVIDER_ENDPOINT", "https://maps.example.test")
    monkeypatch.setenv("ARIEL_MAPS_PROVIDER_TIMEOUT_SECONDS", "6.5")
    monkeypatch.setenv("ARIEL_MAPS_PROVIDER_API_KEY_ENC", "encrypted-maps-key")
    monkeypatch.setenv("ARIEL_WEATHER_PROVIDER_MODE", "dev_fallback")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_ENDPOINT", "https://weather.example.test")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_API_KEY", "weather-key")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_ENDPOINT", "https://wttr.example.test")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_TIMEOUT_SECONDS", "8.5")
    monkeypatch.setenv("ARIEL_WEATHER_DEFAULT_LOCATION", "Austin, TX")

    settings = _app_settings_without_env_files()

    assert settings.terminal_dir == "/tmp/ariel-terminal"
    assert settings.search_brave_base_url == "https://search.example.test/res/v1"
    assert settings.search_web_timeout_seconds == 3.5
    assert settings.search_web_api_key == "search-key"
    assert settings.search_news_timeout_seconds == 4.5
    assert settings.search_news_api_key == "news-key"
    assert settings.web_extract_provider_endpoint == "https://extract.example.test"
    assert settings.web_extract_timeout_seconds == 5.5
    assert settings.web_extract_max_retries == 4
    assert settings.web_extract_api_key == "extract-key"
    assert settings.maps_provider_endpoint == "https://maps.example.test"
    assert settings.maps_provider_timeout_seconds == 6.5
    assert settings.maps_provider_api_key_enc == "encrypted-maps-key"
    assert settings.weather_provider_mode == "dev_fallback"
    assert settings.weather_production_endpoint == "https://weather.example.test"
    assert settings.weather_production_timeout_seconds == 7.5
    assert settings.weather_production_api_key == "weather-key"
    assert settings.weather_dev_endpoint == "https://wttr.example.test"
    assert settings.weather_dev_timeout_seconds == 8.5
    assert settings.weather_default_location == "Austin, TX"


@pytest.mark.parametrize(
    "env_name",
    [
        "ARIEL_SEARCH_WEB_TIMEOUT_SECONDS",
        "ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS",
        "ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS",
        "ARIEL_MAPS_PROVIDER_TIMEOUT_SECONDS",
        "ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS",
        "ARIEL_WEATHER_DEV_TIMEOUT_SECONDS",
    ],
)
def test_provider_timeout_settings_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


@pytest.mark.parametrize(
    "env_value",
    ["-1", "6"],
)
def test_web_extract_max_retries_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
) -> None:
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_MAX_RETRIES", env_value)

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


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
        "ARIEL_TERMINAL_RUN_TIMEOUT_SECONDS",
        "ARIEL_TERMINAL_TIMEOUT_KILL_AFTER_SECONDS",
        "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS",
        "ARIEL_TERMINAL_OUTPUT_LIMIT_BYTES",
        "ARIEL_TERMINAL_BACKGROUND_TIMEOUT_SECONDS",
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
