from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

import pytest
from pydantic import ValidationError

from ariel import app as app_module
from ariel.agency_daemon import AgencyDaemonError
from ariel.app import create_app
from ariel.config import AppSettings
from ariel.persistence import MEMORY_EMBEDDING_DIMENSIONS

STRONG_LOCAL_AUTH_TOKEN = "test_local_auth_token_0123456789abcdef"
CONNECTOR_KEYRING = '{"v1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}'


def _app_settings_without_env_files() -> AppSettings:
    return cast(Any, AppSettings)(_env_file=None)


def test_agency_runtime_binding_requires_reachable_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "agency.sock"
    socket_path_text = str(socket_path)
    settings = cast(Any, AppSettings)(
        _env_file=None,
        agency_allowed_repo_roots=str(tmp_path),
        agency_socket_path=str(socket_path),
        agency_timeout_seconds=30.0,
    )

    assert app_module._agency_runtime_is_bound(settings) is False

    socket_path.touch()

    class ReachableAgencyDaemonClient:
        def __init__(self, *, socket_path: str, timeout_seconds: float) -> None:
            assert socket_path == socket_path_text
            assert timeout_seconds == 1.0

        def health(self) -> dict[str, object]:
            return {"ok": True, "api_version": 3}

    monkeypatch.setattr(app_module, "AgencyDaemonClient", ReachableAgencyDaemonClient)
    assert app_module._agency_runtime_is_bound(settings) is True

    class UnreachableAgencyDaemonClient:
        def __init__(self, *, socket_path: str, timeout_seconds: float) -> None:
            del socket_path, timeout_seconds

        def health(self) -> dict[str, object]:
            raise AgencyDaemonError("agency daemon unavailable")

    monkeypatch.setattr(app_module, "AgencyDaemonClient", UnreachableAgencyDaemonClient)
    assert app_module._agency_runtime_is_bound(settings) is False


@pytest.mark.uses_real_env_files
def test_app_settings_honors_ariel_env_file_override(tmp_path: Path) -> None:
    env_file = tmp_path / "ariel.env"
    env_file.write_text(
        "\n".join(
            [
                "ARIEL_DATABASE_URL=postgresql+psycopg://dev-user:dev-pass@localhost/dev-db",
                "ARIEL_BIND_PORT=8123",
                "ARIEL_MODEL_NAME=env-file-model",
            ]
        ),
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("ARIEL_")}
    env["ARIEL_ENV_FILE"] = str(env_file)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json\n"
                "from ariel.config import AppSettings\n"
                "settings = AppSettings()\n"
                "print(json.dumps({\n"
                "    'database_url': settings.database_url,\n"
                "    'bind_port': settings.bind_port,\n"
                "    'model_name': settings.model_name,\n"
                "}))\n"
            ),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "database_url": "postgresql+psycopg://dev-user:dev-pass@localhost/dev-db",
        "bind_port": 8123,
        "model_name": "env-file-model",
    }


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


def test_turn_budget_defaults_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIEL_AUTO_ROTATE_MAX_TURNS", raising=False)
    monkeypatch.delenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_MAX_RESPONSE_TOKENS", raising=False)
    monkeypatch.delenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", raising=False)
    monkeypatch.delenv("ARIEL_AGENT_LOOP_LIVE_ROUNDS", raising=False)
    monkeypatch.delenv("ARIEL_MEMORY_RECALL_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_MEMORY_ENCODE_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_MEMORY_DREAM_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_MEMORY_DREAM_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ARIEL_APPROVAL_ACTOR_ID", raising=False)

    settings = AppSettings.model_validate({})
    assert settings.auto_rotate_max_turns == 120
    assert settings.auto_rotate_max_age_seconds == 172800
    assert settings.max_response_tokens == 700
    assert settings.main_turn_budget_seconds == 180.0
    assert settings.agent_loop_max_model_calls == 50
    assert settings.agent_loop_live_rounds == 8
    assert settings.memory_recall_budget_seconds == 60.0
    assert settings.memory_encode_budget_seconds == 60.0
    assert settings.memory_dream_budget_seconds == 600.0
    assert settings.memory_dream_interval_seconds == 86400.0
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
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_TURNS", "77")
    monkeypatch.setenv("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "2222")
    monkeypatch.setenv("ARIEL_MAX_RESPONSE_TOKENS", "321")
    monkeypatch.setenv("ARIEL_MAIN_TURN_BUDGET_SECONDS", "300.0")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "100")
    monkeypatch.setenv("ARIEL_AGENT_LOOP_LIVE_ROUNDS", "4")
    monkeypatch.setenv("ARIEL_APPROVAL_TTL_SECONDS", "1200")
    monkeypatch.setenv("ARIEL_APPROVAL_ACTOR_ID", "user.integration")

    settings = AppSettings()
    assert settings.auto_rotate_max_turns == 77
    assert settings.auto_rotate_max_age_seconds == 2222
    assert settings.max_response_tokens == 321
    assert settings.main_turn_budget_seconds == 300.0
    assert settings.agent_loop_max_model_calls == 100
    assert settings.agent_loop_live_rounds == 4
    assert settings.approval_ttl_seconds == 1200
    assert settings.approval_actor_id == "user.integration"


def test_memory_runtime_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_MODEL", "fixture-embedding")
    monkeypatch.setenv("ARIEL_MEMORY_RECALL_BUDGET_SECONDS", "30.0")
    monkeypatch.setenv("ARIEL_MEMORY_ENCODE_BUDGET_SECONDS", "45.0")
    monkeypatch.setenv("ARIEL_MEMORY_DREAM_BUDGET_SECONDS", "1200.0")
    monkeypatch.setenv("ARIEL_MEMORY_DREAM_INTERVAL_SECONDS", "3600.0")

    settings = AppSettings()
    assert settings.memory_embedding_provider == "local"
    assert settings.memory_embedding_model == "fixture-embedding"
    assert settings.memory_recall_budget_seconds == 30.0
    assert settings.memory_encode_budget_seconds == 45.0
    assert settings.memory_dream_budget_seconds == 1200.0
    assert settings.memory_dream_interval_seconds == 3600.0


def test_memory_embedding_dimensions_must_match_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_MEMORY_EMBEDDING_DIMENSIONS", str(MEMORY_EMBEDDING_DIMENSIONS + 1))

    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize(
    "env_name",
    ["ARIEL_MEMORY_EMBEDDING_PROVIDER", "ARIEL_MEMORY_EMBEDDING_MODEL"],
)
def test_memory_embedding_text_settings_reject_blank_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "   ")

    with pytest.raises(ValidationError, match="memory embedding settings must not be blank"):
        AppSettings()


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("ARIEL_AUTO_ROTATE_MAX_TURNS", "0"),
        ("ARIEL_AUTO_ROTATE_MAX_AGE_SECONDS", "0"),
        ("ARIEL_MAX_RESPONSE_TOKENS", "0"),
        ("ARIEL_MAIN_TURN_BUDGET_SECONDS", "0"),
        ("ARIEL_AGENT_LOOP_MAX_MODEL_CALLS", "0"),
        ("ARIEL_AGENT_LOOP_LIVE_ROUNDS", "0"),
        ("ARIEL_MEMORY_RECALL_BUDGET_SECONDS", "0"),
        ("ARIEL_MEMORY_ENCODE_BUDGET_SECONDS", "0"),
        ("ARIEL_MEMORY_DREAM_BUDGET_SECONDS", "0"),
        ("ARIEL_MEMORY_DREAM_INTERVAL_SECONDS", "0"),
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


def test_provider_runtime_settings_default_to_production_values() -> None:
    settings = _app_settings_without_env_files()

    assert settings.search_brave_base_url == "https://api.search.brave.com/res/v1"
    assert settings.search_web_timeout_seconds == 8.0
    assert settings.search_web_api_key is None
    assert settings.search_news_timeout_seconds == 8.0
    assert settings.web_extract_provider_endpoint is None
    assert settings.web_extract_timeout_seconds == 10.0
    assert settings.web_extract_max_retries == 2
    assert settings.maps_api_key is None
    assert settings.maps_timeout_seconds == 8.0
    assert settings.weather_provider_mode == "production"
    assert settings.weather_production_endpoint == "https://api.tomorrow.io/v4/weather/forecast"
    assert settings.weather_production_timeout_seconds == 8.0
    assert settings.weather_production_api_key is None
    assert settings.weather_dev_endpoint == "https://wttr.in"
    assert settings.weather_dev_timeout_seconds == 8.0
    assert settings.weather_default_location is None


def test_provider_runtime_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_SEARCH_BRAVE_BASE_URL", "https://search.example.test/res/v1")
    monkeypatch.setenv("ARIEL_SEARCH_WEB_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("ARIEL_SEARCH_WEB_API_KEY", "search-key")
    monkeypatch.setenv("ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_PROVIDER_ENDPOINT", "https://extract.example.test")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("ARIEL_WEB_EXTRACT_MAX_RETRIES", "4")
    monkeypatch.setenv("ARIEL_MAPS_API_KEY", "maps-key")
    monkeypatch.setenv("ARIEL_MAPS_TIMEOUT_SECONDS", "6.5")
    monkeypatch.setenv("ARIEL_WEATHER_PROVIDER_MODE", "dev")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_ENDPOINT", "https://weather.example.test")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ARIEL_WEATHER_PRODUCTION_API_KEY", "weather-key")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_ENDPOINT", "https://wttr.example.test")
    monkeypatch.setenv("ARIEL_WEATHER_DEV_TIMEOUT_SECONDS", "8.5")
    monkeypatch.setenv("ARIEL_WEATHER_DEFAULT_LOCATION", "Austin, TX")

    settings = _app_settings_without_env_files()

    assert settings.search_brave_base_url == "https://search.example.test/res/v1"
    assert settings.search_web_timeout_seconds == 3.5
    assert settings.search_web_api_key == "search-key"
    assert settings.search_news_timeout_seconds == 4.5
    assert settings.web_extract_provider_endpoint == "https://extract.example.test"
    assert settings.web_extract_timeout_seconds == 5.5
    assert settings.web_extract_max_retries == 4
    assert settings.maps_api_key == "maps-key"
    assert settings.maps_timeout_seconds == 6.5
    assert settings.weather_provider_mode == "dev"
    assert settings.weather_production_endpoint == "https://weather.example.test"
    assert settings.weather_production_timeout_seconds == 7.5
    assert settings.weather_production_api_key == "weather-key"
    assert settings.weather_dev_endpoint == "https://wttr.example.test"
    assert settings.weather_dev_timeout_seconds == 8.5
    assert settings.weather_default_location == "Austin, TX"


def test_attachment_scanner_mode_normalizes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", " FAIL_CLOSED ")

    settings = _app_settings_without_env_files()

    assert settings.attachment_scanner_mode == "fail_closed"


def test_attachment_scanner_mode_rejects_unknown_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_ATTACHMENT_SCANNER_MODE", "permissive")

    with pytest.raises(ValidationError, match="attachment_scanner_mode"):
        _app_settings_without_env_files()


@pytest.mark.parametrize(
    "env_name",
    [
        "ARIEL_SEARCH_WEB_TIMEOUT_SECONDS",
        "ARIEL_SEARCH_NEWS_TIMEOUT_SECONDS",
        "ARIEL_WEB_EXTRACT_TIMEOUT_SECONDS",
        "ARIEL_MAPS_TIMEOUT_SECONDS",
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
        "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS",
    ],
)
def test_worker_and_agency_numeric_settings_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


def test_public_webhook_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", "https://ariel.example.com/")

    settings = _app_settings_without_env_files()

    assert settings.public_webhook_base_url == "https://ariel.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://ariel.example.com",
        "https://ariel.example.com/foo",
        "https://ariel.example.com/?q=1",
        "https://ariel.example.com/#frag",
        "ariel.example.com",
    ],
)
def test_public_webhook_base_url_rejects_non_clean_https(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", value)

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()


def test_production_requires_public_webhook_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIEL_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", STRONG_LOCAL_AUTH_TOKEN)
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_SECRET", "prod-not-default")
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_KEYS", CONNECTOR_KEYRING)

    with pytest.raises(ValidationError, match="public_webhook_base_url is required in production"):
        _app_settings_without_env_files()


def test_google_pubsub_subscription_rejects_non_resource_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", "projects/my-project/topics/topic")
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION", "not-a-resource-path")
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "/etc/sa.json")

    with pytest.raises(ValidationError, match="google_pubsub_subscription"):
        _app_settings_without_env_files()


def test_google_pubsub_topic_rejects_non_resource_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", "not-a-resource-path")
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
        "projects/my-project/subscriptions/ariel-gmail-watch-sub",
    )
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "/etc/sa.json")

    with pytest.raises(ValidationError, match="google_pubsub_topic"):
        _app_settings_without_env_files()


def test_google_application_credentials_path_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", "projects/my-project/topics/topic")
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
        "projects/my-project/subscriptions/ariel-gmail-watch-sub",
    )
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "relative/sa.json")

    with pytest.raises(ValidationError, match="absolute"):
        _app_settings_without_env_files()


@pytest.mark.parametrize(
    "topic, subscription, credentials_path",
    [
        ("projects/my-project/topics/topic", "projects/my-project/subscriptions/sub", None),
        ("projects/my-project/topics/topic", None, "/etc/sa.json"),
        (None, "projects/my-project/subscriptions/sub", "/etc/sa.json"),
    ],
)
def test_pubsub_settings_must_be_set_together(
    monkeypatch: pytest.MonkeyPatch,
    topic: str | None,
    subscription: str | None,
    credentials_path: str | None,
) -> None:
    if topic is not None:
        monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", topic)
    if subscription is not None:
        monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION", subscription)
    if credentials_path is not None:
        monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", credentials_path)

    with pytest.raises(ValidationError, match="must be set together"):
        _app_settings_without_env_files()


def test_pubsub_settings_set_together_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_GOOGLE_PUBSUB_TOPIC", "projects/my-project/topics/topic")
    monkeypatch.setenv(
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
        "projects/my-project/subscriptions/ariel-gmail-watch-sub",
    )
    monkeypatch.setenv("ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH", "/etc/sa.json")

    settings = _app_settings_without_env_files()

    assert settings.google_pubsub_topic == "projects/my-project/topics/topic"
    assert settings.google_pubsub_subscription == (
        "projects/my-project/subscriptions/ariel-gmail-watch-sub"
    )
    assert settings.google_application_credentials_path == "/etc/sa.json"


def test_production_requires_google_redirect_to_match_public_webhook_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", STRONG_LOCAL_AUTH_TOKEN)
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_SECRET", "prod-not-default")
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_KEYS", CONNECTOR_KEYRING)
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", "https://ariel.example.com")
    monkeypatch.setenv("ARIEL_GOOGLE_OAUTH_REDIRECT_URI", "https://other.example.com/callback")
    monkeypatch.setenv("ARIEL_AGENCY_SOCKET_PATH", "/tmp/agencyd.sock")
    monkeypatch.setenv("ARIEL_AGENCY_ALLOWED_REPO_ROOTS", "/opt/ariel,/opt/agency")

    with pytest.raises(ValidationError, match="google_oauth_redirect_uri must equal"):
        _app_settings_without_env_files()


def test_production_requires_absolute_agency_socket_and_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARIEL_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ARIEL_LOCAL_AUTH_TOKEN", STRONG_LOCAL_AUTH_TOKEN)
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_SECRET", "prod-not-default")
    monkeypatch.setenv("ARIEL_CONNECTOR_ENCRYPTION_KEYS", CONNECTOR_KEYRING)
    monkeypatch.setenv("ARIEL_PUBLIC_WEBHOOK_BASE_URL", "https://ariel.example.com")
    monkeypatch.setenv(
        "ARIEL_GOOGLE_OAUTH_REDIRECT_URI",
        "https://ariel.example.com/v1/connectors/google/callback",
    )
    monkeypatch.setenv("ARIEL_AGENCY_SOCKET_PATH", "relative.sock")
    monkeypatch.setenv("ARIEL_AGENCY_ALLOWED_REPO_ROOTS", "/opt/ariel,/opt/agency")

    with pytest.raises(ValidationError, match="agency_socket_path"):
        _app_settings_without_env_files()

    monkeypatch.setenv("ARIEL_AGENCY_SOCKET_PATH", "/tmp/agencyd.sock")
    monkeypatch.setenv("ARIEL_AGENCY_ALLOWED_REPO_ROOTS", "relative")

    with pytest.raises(ValidationError, match="agency_allowed_repo_roots"):
        _app_settings_without_env_files()


@pytest.mark.parametrize(
    "env_name",
    [
        "ARIEL_SUBSCRIBER_HEARTBEAT_INTERVAL_SECONDS",
        "ARIEL_SUBSCRIBER_HEARTBEAT_STALENESS_FACTOR",
    ],
)
def test_subscriber_heartbeat_settings_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ValidationError):
        _app_settings_without_env_files()
