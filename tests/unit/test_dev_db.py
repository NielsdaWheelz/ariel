from __future__ import annotations

from pathlib import Path

import pytest

from ariel.config import ENV_FILE_SELECTOR_ENV_VAR
from ariel.dev_db import (
    load_local_env,
    parse_dotenv_file,
    redact_command_for_display,
    redact_database_url,
    resolve_local_postgres_runtime,
)


def test_parse_dotenv_file_reads_key_values_and_ignores_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "ARIEL_DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db",
                "export ARIEL_OPENAI_API_KEY=test-key",
                "IGNORED_LINE_WITHOUT_EQUALS",
                "ARIEL_MODEL_REASONING_EFFORT='high'",
            ]
        ),
        encoding="utf-8",
    )

    values = parse_dotenv_file(env_file)

    assert values["ARIEL_DATABASE_URL"] == "postgresql+psycopg://u:p@localhost:5432/db"
    assert values["ARIEL_OPENAI_API_KEY"] == "test-key"
    assert values["ARIEL_MODEL_REASONING_EFFORT"] == "high"
    assert "IGNORED_LINE_WITHOUT_EQUALS" not in values


def test_load_local_env_prefers_env_file_then_os_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ARIEL_MODEL_REASONING_EFFORT=medium\nARIEL_OPENAI_API_KEY=base\n"
    )
    (tmp_path / ".env.local").write_text("ARIEL_OPENAI_API_KEY=local\n")

    merged = load_local_env(
        tmp_path,
        environ={"ARIEL_MODEL_REASONING_EFFORT": "low"},
    )

    assert merged["ARIEL_OPENAI_API_KEY"] == "local"
    assert merged["ARIEL_MODEL_REASONING_EFFORT"] == "low"


def test_load_local_env_honors_ariel_env_file_override(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ARIEL_DATABASE_URL=prod-url\n")
    (tmp_path / ".env.local").write_text("ARIEL_DATABASE_URL=prod-url\n")
    (tmp_path / ".env.dev").write_text("ARIEL_DATABASE_URL=dev-url\n")

    merged = load_local_env(
        tmp_path,
        environ={ENV_FILE_SELECTOR_ENV_VAR: ".env.dev"},
    )

    assert merged["ARIEL_DATABASE_URL"] == "dev-url"
    assert merged[ENV_FILE_SELECTOR_ENV_VAR] == ".env.dev"


def test_load_local_env_ariel_env_file_override_accepts_absolute_path(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("ARIEL_DATABASE_URL=custom-url\n")
    # .env.local should be bypassed.
    (tmp_path / ".env.local").write_text("ARIEL_DATABASE_URL=prod-url\n")

    merged = load_local_env(
        tmp_path,
        environ={ENV_FILE_SELECTOR_ENV_VAR: str(env_file)},
    )

    assert merged["ARIEL_DATABASE_URL"] == "custom-url"


def test_load_local_env_rejects_missing_ariel_env_file_override(tmp_path: Path) -> None:
    missing_env_file = tmp_path / "missing.env"

    with pytest.raises(FileNotFoundError, match="ARIEL_ENV_FILE points to missing env file"):
        load_local_env(
            tmp_path,
            environ={ENV_FILE_SELECTOR_ENV_VAR: str(missing_env_file)},
        )


def test_load_local_env_ignores_blank_ariel_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("ARIEL_DATABASE_URL=prod-url\n")

    merged = load_local_env(
        tmp_path,
        environ={ENV_FILE_SELECTOR_ENV_VAR: "   "},
    )

    assert merged["ARIEL_DATABASE_URL"] == "prod-url"


def test_resolve_local_postgres_runtime_uses_connection_string_values() -> None:
    runtime = resolve_local_postgres_runtime(
        {
            "ARIEL_DATABASE_URL": "postgresql+psycopg://myuser:mypass@localhost:5544/mydb",
            "ARIEL_DB_CONTAINER_NAME": "custom-container",
            "ARIEL_DB_DOCKER_IMAGE": "postgres:17",
            "ARIEL_DB_VOLUME_NAME": "custom-volume",
        }
    )

    assert runtime.user == "myuser"
    assert runtime.password == "mypass"
    assert runtime.database == "mydb"
    assert runtime.host_port == 5544
    assert runtime.container_name == "custom-container"
    assert runtime.image == "postgres:17"
    assert runtime.volume_name == "custom-volume"


def test_resolve_local_postgres_runtime_rejects_non_loopback_host() -> None:
    with pytest.raises(ValueError, match="loopback"):
        resolve_local_postgres_runtime(
            {"ARIEL_DATABASE_URL": "postgresql+psycopg://user:pass@db.internal:5432/ariel"}
        )


def test_redact_database_url_hides_password() -> None:
    assert (
        redact_database_url("postgresql+psycopg://myuser:mypass@localhost:5544/mydb")
        == "postgresql+psycopg://myuser:***@localhost:5544/mydb"
    )


def test_redact_database_url_leaves_passwordless_url_alone() -> None:
    assert redact_database_url("postgresql+psycopg://localhost/ariel") == (
        "postgresql+psycopg://localhost/ariel"
    )


def test_redact_command_for_display_hides_postgres_password() -> None:
    assert redact_command_for_display(
        ["docker", "run", "-e", "POSTGRES_PASSWORD=secret-value", "postgres:17"]
    ) == ["docker", "run", "-e", "POSTGRES_PASSWORD=***", "postgres:17"]
