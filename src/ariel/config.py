from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIEL_",
        extra="ignore",
        env_file=(
            _PROJECT_ROOT / ".env",
            _PROJECT_ROOT / ".env.local",
        ),
        env_file_encoding="utf-8",
    )

    database_url: str = "postgresql+psycopg://localhost/ariel"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    model_name: str = "gpt-5.5"
    openai_api_key: str | None = None
    model_timeout_seconds: float = 30.0
    model_reasoning_effort: str = "medium"
    model_verbosity: str = "low"
    max_recent_turns: int = 12
    max_recalled_memories: int = 8
    max_context_tokens: int = 6000
    auto_rotate_max_turns: int = 120
    auto_rotate_max_age_seconds: int = 172800
    auto_rotate_context_pressure_tokens: int = 5400
    max_response_tokens: int = 700
    max_model_attempts: int = 2
    max_turn_wall_time_ms: int = 20000
    approval_ttl_seconds: int = 900
    approval_actor_id: str = "user.local"
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str = "http://127.0.0.1:8000/v1/connectors/google/callback"
    google_oauth_state_ttl_seconds: int = 600
    google_oauth_timeout_seconds: float = 10.0
    connector_encryption_secret: str = "dev-local-connector-secret"
    connector_encryption_key_version: str = "v1"
    connector_encryption_keys: str | None = None
    discord_bot_token: str | None = None
    discord_guild_id: int | None = None
    discord_channel_id: int | None = None
    discord_user_id: int | None = None
    discord_ariel_base_url: str = "http://127.0.0.1:8000"
    discord_notification_timeout_seconds: float = 10.0
    agency_socket_path: str = "/tmp/agency-daemon.sock"
    agency_allowed_repo_roots: str = ""
    agency_default_base_branch: str = "main"
    agency_default_runner: str = "codex"
    agency_timeout_seconds: float = 30.0
    agency_event_secret: str | None = None
    agency_event_max_skew_seconds: int = 300
    worker_poll_seconds: float = 1.0
    worker_heartbeat_timeout_seconds: int = 300

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

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_openai_api_key_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

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

    @field_validator("max_recent_turns")
    @classmethod
    def _max_recent_turns_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_recent_turns must be >= 1")
        return value

    @field_validator("max_recalled_memories")
    @classmethod
    def _max_recalled_memories_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_recalled_memories must be >= 1")
        return value

    @field_validator("max_context_tokens")
    @classmethod
    def _max_context_tokens_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_context_tokens must be >= 1")
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

    @field_validator("auto_rotate_context_pressure_tokens")
    @classmethod
    def _auto_rotate_context_pressure_tokens_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("auto_rotate_context_pressure_tokens must be >= 1")
        return value

    @field_validator("max_response_tokens")
    @classmethod
    def _max_response_tokens_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_response_tokens must be >= 1")
        return value

    @field_validator("max_model_attempts")
    @classmethod
    def _max_model_attempts_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_model_attempts must be >= 1")
        return value

    @field_validator("max_turn_wall_time_ms")
    @classmethod
    def _max_turn_wall_time_ms_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_turn_wall_time_ms must be >= 1")
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
        "agency_timeout_seconds",
        "worker_poll_seconds",
    )
    @classmethod
    def _positive_float_settings_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout and poll settings must be > 0")
        return value

    @field_validator(
        "agency_event_max_skew_seconds",
        "worker_heartbeat_timeout_seconds",
    )
    @classmethod
    def _positive_int_settings_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agency and worker integer settings must be >= 1")
        return value
