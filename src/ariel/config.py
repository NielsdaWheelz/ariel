from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path

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
    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    model_api_base_url: str = "https://api.openai.com/v1"
    model_api_key: str | None = None
    model_timeout_seconds: float = 30.0
    max_recent_turns: int = 12
    max_context_tokens: int = 6000
    max_response_tokens: int = 700
    max_model_attempts: int = 2
    max_turn_wall_time_ms: int = 20000
    approval_ttl_seconds: int = 900
    approval_actor_id: str = "user.local"

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

    @field_validator("model_provider")
    @classmethod
    def _model_provider_must_be_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"openai", "echo"}:
            raise ValueError("model_provider must be one of: openai, echo")
        return normalized

    @field_validator("max_recent_turns")
    @classmethod
    def _max_recent_turns_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_recent_turns must be >= 1")
        return value

    @field_validator("max_context_tokens")
    @classmethod
    def _max_context_tokens_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_context_tokens must be >= 1")
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
