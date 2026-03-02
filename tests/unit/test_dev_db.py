from __future__ import annotations

from pathlib import Path

import pytest

from ariel.dev_db import (
    load_local_env,
    parse_dotenv_file,
    resolve_local_postgres_runtime,
)


def test_parse_dotenv_file_reads_key_values_and_ignores_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "ARIEL_DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db",
                "export ARIEL_MODEL_PROVIDER=echo",
                "IGNORED_LINE_WITHOUT_EQUALS",
                "ARIEL_MODEL_NAME='echo-v1'",
            ]
        ),
        encoding="utf-8",
    )

    values = parse_dotenv_file(env_file)

    assert values["ARIEL_DATABASE_URL"] == "postgresql+psycopg://u:p@localhost:5432/db"
    assert values["ARIEL_MODEL_PROVIDER"] == "echo"
    assert values["ARIEL_MODEL_NAME"] == "echo-v1"
    assert "IGNORED_LINE_WITHOUT_EQUALS" not in values


def test_load_local_env_prefers_env_file_then_os_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ARIEL_MODEL_PROVIDER=openai\nARIEL_MODEL_NAME=gpt-4o-mini\n")
    (tmp_path / ".env.local").write_text("ARIEL_MODEL_PROVIDER=echo\n")

    merged = load_local_env(
        tmp_path,
        environ={"ARIEL_MODEL_NAME": "echo-v2"},
    )

    assert merged["ARIEL_MODEL_PROVIDER"] == "echo"
    assert merged["ARIEL_MODEL_NAME"] == "echo-v2"


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
