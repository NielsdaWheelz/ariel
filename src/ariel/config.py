from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ariel.google_connector import ConnectorTokenCipher


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_EMBEDDING_DIMENSIONS = 1536
_LOCAL_AUTH_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_PUBSUB_SUBSCRIPTION_PATTERN = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/subscriptions/[A-Za-z][A-Za-z0-9_.~+%-]{2,254}$"
)
_ENV_FILES = (_PROJECT_ROOT / ".env", _PROJECT_ROOT / ".env.local")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIEL_",
        extra="ignore",
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
    )

    database_url: str = "postgresql+psycopg://localhost/ariel"
    deployment_mode: str = "development"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    local_auth_required: bool = False
    local_auth_token: str | None = None
    model_name: str = "gpt-5.5"
    openai_api_key: str | None = None
    model_timeout_seconds: float = 30.0
    model_reasoning_effort: str = "medium"
    model_verbosity: str = "low"
    memory_embedding_provider: str = "openai"
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_dimensions: int = MEMORY_EMBEDDING_DIMENSIONS
    auto_rotate_max_turns: int = 120
    auto_rotate_max_age_seconds: int = 172800
    max_response_tokens: int = 700
    main_turn_budget_seconds: float = 180.0
    research_run_budget_seconds: float = 300.0
    memory_recall_budget_seconds: float = 60.0
    memory_encode_budget_seconds: float = 60.0
    memory_dream_budget_seconds: float = 600.0
    memory_dream_interval_seconds: float = 86400.0
    agent_loop_max_model_calls: int = 50
    agent_loop_live_rounds: int = 8
    approval_ttl_seconds: int = 900
    approval_actor_id: str = "user.local"
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str = "http://127.0.0.1:8000/v1/connectors/google/callback"
    google_oauth_state_ttl_seconds: int = 600
    google_oauth_timeout_seconds: float = 10.0
    google_provider_event_token: str | None = None
    google_pubsub_topic: str | None = None
    public_webhook_base_url: str | None = None
    google_pubsub_subscription: str | None = None
    google_application_credentials_path: str | None = None
    subscriber_heartbeat_interval_seconds: float = 30.0
    subscriber_heartbeat_staleness_factor: float = 2.0
    provider_reconcile_sync_interval_seconds: int = 3600
    connector_encryption_secret: str = "dev-local-connector-secret"
    connector_encryption_key_version: str = "v1"
    connector_encryption_keys: str | None = None
    discord_bot_token: str | None = None
    discord_guild_id: int | None = None
    discord_channel_id: int | None = None
    discord_user_id: int | None = None
    discord_ariel_base_url: str = "http://127.0.0.1:8000"
    discord_notification_timeout_seconds: float = 10.0
    attachment_blob_store_path: str = ".ariel/attachment-blobs"
    attachment_max_bytes: int = 25 * 1024 * 1024
    attachment_fetch_timeout_seconds: float = 10.0
    attachment_handle_ttl_seconds: int = 3600
    attachment_scanner_mode: str = "fail_closed"
    attachment_openai_model: str = "gpt-5.5"
    attachment_openai_audio_model: str = "gpt-4o-transcribe"
    attachment_openai_timeout_seconds: float = 30.0
    agency_socket_path: str = "/tmp/agency-daemon.sock"
    agency_allowed_repo_roots: str = ""
    agency_default_base_branch: str = "main"
    agency_default_runner: str = "codex"
    agency_timeout_seconds: float = 30.0
    agency_event_secret: str | None = None
    agency_event_max_skew_seconds: int = 300
    search_brave_base_url: str = "https://api.search.brave.com/res/v1"
    search_web_timeout_seconds: float = 8.0
    search_web_api_key: str | None = None
    search_news_timeout_seconds: float = 8.0
    search_news_api_key: str | None = None
    web_extract_provider_endpoint: str | None = None
    web_extract_timeout_seconds: float = 10.0
    web_extract_max_retries: int = 2
    web_extract_api_key: str | None = None
    maps_api_key: str | None = None
    maps_timeout_seconds: float = 8.0
    home_address: str | None = None
    weather_provider_mode: str = "production"
    weather_production_endpoint: str = "https://api.tomorrow.io/v4/weather/forecast"
    weather_production_timeout_seconds: float = 8.0
    weather_production_api_key: str | None = None
    weather_dev_endpoint: str = "https://wttr.in"
    weather_dev_timeout_seconds: float = 8.0
    weather_default_location: str | None = None
    worker_poll_seconds: float = 1.0

    @field_validator("bind_host")
    @classmethod
    def _bind_host_must_be_loopback(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"localhost", "127.0.0.1", "::1"}:
            return normalized
        try:
            if ip_address(normalized).is_loopback:
                return normalized
        except ValueError:
            pass
        raise ValueError("bind_host must be loopback-only (localhost, 127.0.0.1, or ::1)")

    @field_validator("deployment_mode")
    @classmethod
    def _deployment_mode_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"development", "production"}:
            raise ValueError("deployment_mode must be one of: development, production")
        return normalized

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_openai_api_key_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "search_web_api_key",
        "search_news_api_key",
        "web_extract_provider_endpoint",
        "web_extract_api_key",
        "maps_api_key",
        "weather_production_api_key",
        "weather_default_location",
        "home_address",
        "google_pubsub_topic",
        "public_webhook_base_url",
        "google_pubsub_subscription",
        "google_application_credentials_path",
        mode="before",
    )
    @classmethod
    def _blank_optional_strings_are_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("public_webhook_base_url")
    @classmethod
    def _public_webhook_base_url_must_be_clean_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("public_webhook_base_url must be an https:// URL with a host")
        if parsed.path or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("public_webhook_base_url must have no path, query, or fragment")
        return normalized

    @field_validator("google_pubsub_subscription")
    @classmethod
    def _google_pubsub_subscription_must_be_resource_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _PUBSUB_SUBSCRIPTION_PATTERN.fullmatch(normalized):
            raise ValueError(
                "google_pubsub_subscription must match projects/<project>/subscriptions/<name>"
            )
        return normalized

    @field_validator("google_application_credentials_path")
    @classmethod
    def _google_application_credentials_path_must_be_absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not Path(normalized).is_absolute():
            raise ValueError("google_application_credentials_path must be an absolute path")
        return normalized

    @field_validator("weather_provider_mode")
    @classmethod
    def _weather_provider_mode_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"production", "dev_fallback"}:
            raise ValueError("weather_provider_mode must be production or dev_fallback")
        return normalized

    @field_validator(
        "search_brave_base_url",
        "weather_production_endpoint",
        "weather_dev_endpoint",
    )
    @classmethod
    def _provider_endpoint_settings_must_be_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider endpoint settings must be nonblank")
        return normalized

    @field_validator("google_provider_event_token", mode="before")
    @classmethod
    def _blank_google_provider_event_token_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("local_auth_token", mode="before")
    @classmethod
    def _blank_local_auth_token_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("local_auth_token")
    @classmethod
    def _local_auth_token_must_be_strong(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _LOCAL_AUTH_TOKEN_PATTERN.fullmatch(normalized):
            raise ValueError("local_auth_token must be at least 32 URL-safe random characters")
        return normalized

    @model_validator(mode="after")
    def _security_settings_must_match_deployment(self) -> AppSettings:
        if self.local_auth_required and self.local_auth_token is None:
            raise ValueError("local_auth_token is required when local_auth_required is true")
        if self.deployment_mode == "production":
            if not self.local_auth_required:
                raise ValueError("local_auth_required must be true in production")
            if self.connector_encryption_secret == "dev-local-connector-secret":
                raise ValueError(
                    "connector_encryption_secret must not use the dev default in production"
                )
            if self.connector_encryption_keys is None or not self.connector_encryption_keys.strip():
                raise ValueError("connector_encryption_keys is required in production")
            try:
                ConnectorTokenCipher.from_config(
                    active_key_version=self.connector_encryption_key_version,
                    configured_keys=self.connector_encryption_keys,
                    fallback_secret=self.connector_encryption_secret,
                )
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            if self.public_webhook_base_url is None:
                raise ValueError("public_webhook_base_url is required in production")
        if (self.google_pubsub_subscription is None) != (
            self.google_application_credentials_path is None
        ):
            raise ValueError(
                "google_pubsub_subscription and google_application_credentials_path "
                "must be set together (both for Gmail push on, neither for off)"
            )
        return self

    @field_validator("model_reasoning_effort")
    @classmethod
    def _model_reasoning_effort_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"minimal", "low", "medium", "high"}:
            raise ValueError("model_reasoning_effort must be one of: minimal, low, medium, high")
        return normalized

    @field_validator("model_verbosity")
    @classmethod
    def _model_verbosity_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"low", "medium", "high"}:
            raise ValueError("model_verbosity must be one of: low, medium, high")
        return normalized

    @field_validator(
        "memory_embedding_provider",
        "memory_embedding_model",
    )
    @classmethod
    def _memory_projection_text_settings_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory projection settings must not be blank")
        return normalized

    @field_validator("memory_embedding_dimensions")
    @classmethod
    def _memory_embedding_dimensions_must_match_schema(cls, value: int) -> int:
        if value != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError(f"memory_embedding_dimensions must be {MEMORY_EMBEDDING_DIMENSIONS}")
        return value

    @field_validator("auto_rotate_max_turns")
    @classmethod
    def _auto_rotate_max_turns_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("auto_rotate_max_turns must be >= 1")
        return value

    @field_validator("auto_rotate_max_age_seconds")
    @classmethod
    def _auto_rotate_max_age_seconds_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("auto_rotate_max_age_seconds must be >= 1")
        return value

    @field_validator("max_response_tokens")
    @classmethod
    def _max_response_tokens_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_response_tokens must be >= 1")
        return value

    @field_validator("main_turn_budget_seconds")
    @classmethod
    def _main_turn_budget_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("main_turn_budget_seconds must be > 0")
        return value

    @field_validator("research_run_budget_seconds")
    @classmethod
    def _research_run_budget_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("research_run_budget_seconds must be > 0")
        return value

    @field_validator("memory_recall_budget_seconds")
    @classmethod
    def _memory_recall_budget_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("memory_recall_budget_seconds must be > 0")
        return value

    @field_validator("memory_encode_budget_seconds")
    @classmethod
    def _memory_encode_budget_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("memory_encode_budget_seconds must be > 0")
        return value

    @field_validator("memory_dream_budget_seconds")
    @classmethod
    def _memory_dream_budget_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("memory_dream_budget_seconds must be > 0")
        return value

    @field_validator("memory_dream_interval_seconds")
    @classmethod
    def _memory_dream_interval_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("memory_dream_interval_seconds must be > 0")
        return value

    @field_validator("agent_loop_max_model_calls")
    @classmethod
    def _agent_loop_max_model_calls_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent_loop_max_model_calls must be >= 1")
        return value

    @field_validator("agent_loop_live_rounds")
    @classmethod
    def _agent_loop_live_rounds_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent_loop_live_rounds must be >= 1")
        return value

    @field_validator("approval_ttl_seconds")
    @classmethod
    def _approval_ttl_seconds_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("approval_ttl_seconds must be >= 1")
        return value

    @field_validator("approval_actor_id")
    @classmethod
    def _approval_actor_id_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("approval_actor_id must not be blank")
        return normalized

    @field_validator("google_oauth_redirect_uri")
    @classmethod
    def _google_oauth_redirect_uri_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("google_oauth_redirect_uri must not be blank")
        return normalized

    @field_validator("google_oauth_state_ttl_seconds")
    @classmethod
    def _google_oauth_state_ttl_seconds_must_be_positive(cls, value: int) -> int:
        if value < 30:
            raise ValueError("google_oauth_state_ttl_seconds must be >= 30")
        return value

    @field_validator("google_oauth_timeout_seconds")
    @classmethod
    def _google_oauth_timeout_seconds_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("google_oauth_timeout_seconds must be > 0")
        return value

    @field_validator("connector_encryption_secret")
    @classmethod
    def _connector_encryption_secret_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("connector_encryption_secret must not be blank")
        return normalized

    @field_validator("connector_encryption_key_version")
    @classmethod
    def _connector_encryption_key_version_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("connector_encryption_key_version must not be blank")
        return normalized

    @field_validator("discord_bot_token", mode="before")
    @classmethod
    def _blank_discord_bot_token_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agency_event_secret", mode="before")
    @classmethod
    def _blank_agency_event_secret_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agency_socket_path", "agency_default_base_branch", "agency_default_runner")
    @classmethod
    def _agency_text_settings_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("agency settings must not be blank")
        return normalized

    @field_validator(
        "discord_guild_id",
        "discord_channel_id",
        "discord_user_id",
        mode="before",
    )
    @classmethod
    def _blank_discord_id_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "discord_guild_id",
        "discord_channel_id",
        "discord_user_id",
    )
    @classmethod
    def _discord_ids_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("discord ids must be positive integers")
        return value

    @field_validator("discord_ariel_base_url")
    @classmethod
    def _discord_ariel_base_url_must_be_http_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("discord_ariel_base_url must be an http(s) URL")
        return normalized

    @field_validator(
        "discord_notification_timeout_seconds",
        "attachment_fetch_timeout_seconds",
        "attachment_openai_timeout_seconds",
        "agency_timeout_seconds",
        "search_web_timeout_seconds",
        "search_news_timeout_seconds",
        "web_extract_timeout_seconds",
        "maps_timeout_seconds",
        "weather_production_timeout_seconds",
        "weather_dev_timeout_seconds",
        "worker_poll_seconds",
        "subscriber_heartbeat_interval_seconds",
        "subscriber_heartbeat_staleness_factor",
    )
    @classmethod
    def _positive_float_settings_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout, poll, and attachment settings must be > 0")
        return value

    @field_validator(
        "attachment_max_bytes",
        "attachment_handle_ttl_seconds",
        "agency_event_max_skew_seconds",
        "provider_reconcile_sync_interval_seconds",
    )
    @classmethod
    def _positive_int_settings_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agency, attachment, and worker settings must be >= 1")
        return value

    @field_validator("web_extract_max_retries")
    @classmethod
    def _web_extract_max_retries_must_be_bounded(cls, value: int) -> int:
        if value < 0 or value > 5:
            raise ValueError("web_extract_max_retries must be between 0 and 5")
        return value

    @field_validator(
        "attachment_blob_store_path",
        "attachment_openai_model",
        "attachment_openai_audio_model",
    )
    @classmethod
    def _attachment_text_settings_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("attachment settings must not be blank")
        return normalized

    @field_validator("attachment_scanner_mode")
    @classmethod
    def _attachment_scanner_mode_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "fail_closed"}:
            raise ValueError("attachment_scanner_mode must be one of: disabled, fail_closed")
        return normalized
