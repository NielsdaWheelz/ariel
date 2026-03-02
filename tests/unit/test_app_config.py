from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ariel.app import create_app
from ariel.config import AppSettings


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
