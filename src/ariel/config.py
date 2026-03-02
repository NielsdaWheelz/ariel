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
            _PROJECT_ROOT / ".env.local",
            _PROJECT_ROOT / ".env",
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
