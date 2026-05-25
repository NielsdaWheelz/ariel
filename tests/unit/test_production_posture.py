from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from ariel.dev_db import DEV_DB_ENV_VARS
from ariel.production_posture import (
    AGENCY_INSTALL_ROOT,
    ARIEL_SYSTEMD_SERVICES,
    ARIEL_INSTALL_ROOT,
    CADDY_SERVICE,
    REQUIRED_ATTACHMENT_BLOB_STORE_PATH,
    REQUIRED_AGENCY_ENVIRONMENT_SUBSTRINGS,
    REQUIRED_AGENCY_EXECSTART_SUBSTRING,
    REQUIRED_AGENCY_UNIT_PROPERTIES,
    REQUIRED_AGENCY_SOCKET_PATH,
    REQUIRED_CADDYFILE_PATH,
    REQUIRED_CADDY_PUBLIC_PROXY_ROUTES,
    REQUIRED_ENVIRONMENT_FILE,
    MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS,
    REQUIRED_PUBLIC_FIREWALL_PORTS,
    REQUIRED_PRODUCTION_ENV_VARS,
    REQUIRED_UNIT_EXECSTART_SUBSTRINGS,
    REQUIRED_UNIT_PROPERTIES,
    known_environment_names,
    parse_environment_file,
    production_environment_names,
    redacted_local_environment_audit,
    unknown_environment_names,
    validate_agency_daemon_posture,
    validate_api_listener_posture,
    validate_caddy_config_posture,
    validate_caddy_service_posture,
    validate_production_environment_settings,
    validate_production_service_posture,
    validate_required_environment_values,
    validate_ufw_firewall_posture,
)


CONNECTOR_KEYRING = '{"v1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}'
ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "deploy/caddy/Caddyfile"
VERIFY_PRODUCTION_POSTURE = ROOT / "scripts/verify_production_posture.py"


def _canonical_properties() -> dict[str, dict[str, str]]:
    return {
        service: {
            **REQUIRED_UNIT_PROPERTIES,
            "EnvironmentFiles": f"{REQUIRED_ENVIRONMENT_FILE} (ignore_errors=no)",
            "ExecStart": REQUIRED_UNIT_EXECSTART_SUBSTRINGS[service],
        }
        for service in ARIEL_SYSTEMD_SERVICES
    }


def _canonical_agency_properties() -> dict[str, str]:
    return {
        **REQUIRED_AGENCY_UNIT_PROPERTIES,
        "Environment": " ".join(REQUIRED_AGENCY_ENVIRONMENT_SUBSTRINGS),
        "ExecStart": REQUIRED_AGENCY_EXECSTART_SUBSTRING,
    }


def _canonical_production_env() -> dict[str, str]:
    return {
        "ARIEL_DATABASE_URL": "postgresql+psycopg://ariel:password@127.0.0.1:5432/ariel",
        "ARIEL_DEPLOYMENT_MODE": "production",
        "ARIEL_BIND_HOST": "127.0.0.1",
        "ARIEL_BIND_PORT": "8000",
        "ARIEL_LOCAL_AUTH_REQUIRED": "true",
        "ARIEL_LOCAL_AUTH_TOKEN": "test_local_auth_token_0123456789abcdef",
        "ARIEL_CONNECTOR_ENCRYPTION_SECRET": "prod-connector-secret",
        "ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION": "v1",
        "ARIEL_CONNECTOR_ENCRYPTION_KEYS": CONNECTOR_KEYRING,
        "ARIEL_PUBLIC_WEBHOOK_BASE_URL": "https://ariel.example.com",
        "ARIEL_MODEL_TIMEOUT_SECONDS": "30.0",
        "ARIEL_MEMORY_EMBEDDING_DIMENSIONS": "1536",
        "ARIEL_GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "ARIEL_GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret",
        "ARIEL_GOOGLE_OAUTH_REDIRECT_URI": (
            "https://ariel.example.com/v1/connectors/google/callback"
        ),
        "ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS": "600",
        "ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS": "10.0",
        "ARIEL_GOOGLE_PUBSUB_TOPIC": "projects/ariel-prod/topics/ariel-gmail-watch",
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION": (
            "projects/ariel-prod/subscriptions/ariel-gmail-watch-sub"
        ),
        "ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH": ("/etc/ariel/secrets/gcp-pubsub-sa.json"),
        "ARIEL_OPENAI_API_KEY": "openai-key",
        "ARIEL_ANTHROPIC_API_KEY": "anthropic-key",
        "ARIEL_GOOGLE_API_KEY": "google-key",
        "ARIEL_OPENROUTER_API_KEY": "openrouter-key",
        "ARIEL_DISCORD_BOT_TOKEN": "discord-token",
        "ARIEL_DISCORD_GUILD_ID": "123",
        "ARIEL_DISCORD_CHANNEL_ID": "456",
        "ARIEL_DISCORD_USER_ID": "789",
        "ARIEL_DISCORD_ARIEL_BASE_URL": "http://127.0.0.1:8000",
        "ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS": "10.0",
        "ARIEL_AGENCY_SOCKET_PATH": "/var/lib/agency/agencyd.sock",
        "ARIEL_AGENCY_ALLOWED_REPO_ROOTS": f"{ARIEL_INSTALL_ROOT},{AGENCY_INSTALL_ROOT}",
        "ARIEL_AGENCY_DEFAULT_BASE_BRANCH": "main",
        "ARIEL_AGENCY_DEFAULT_RUNNER": "codex",
        "ARIEL_AGENCY_TIMEOUT_SECONDS": "30.0",
        "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS": "300",
        "ARIEL_ATTACHMENT_BLOB_STORE_PATH": REQUIRED_ATTACHMENT_BLOB_STORE_PATH,
        "ARIEL_WORKER_POLL_SECONDS": "1.0",
        "ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS": "3600",
    }


def test_validate_production_service_posture_accepts_canonical_units() -> None:
    assert (
        validate_production_service_posture(
            active_states={service: "active" for service in ARIEL_SYSTEMD_SERVICES},
            enabled_states={service: "enabled" for service in ARIEL_SYSTEMD_SERVICES},
            unit_properties=_canonical_properties(),
        )
        == []
    )


def test_validate_production_service_posture_reports_drift() -> None:
    properties = _canonical_properties()
    properties["ariel-api"]["User"] = "niels"
    properties["ariel-api"]["PrivateTmp"] = "no"
    properties["ariel-worker"]["EnvironmentFiles"] = ""
    properties["ariel-discord"]["ExecStart"] = "/tmp/ariel.discord_bot"

    errors = validate_production_service_posture(
        active_states={service: "active" for service in ARIEL_SYSTEMD_SERVICES},
        enabled_states={
            service: "enabled" if service != "ariel-pubsub" else "disabled"
            for service in ARIEL_SYSTEMD_SERVICES
        },
        unit_properties=properties,
    )

    assert "ariel-api User is 'niels', expected 'ariel'" in errors
    assert "ariel-api PrivateTmp is 'no', expected 'yes'" in errors
    assert "ariel-pubsub enabled state is 'disabled', expected 'enabled'" in errors
    assert "ariel-worker EnvironmentFiles does not include /etc/ariel/ariel.env" in errors
    assert (
        "ariel-discord ExecStart does not include /opt/ariel/.venv/bin/python -m ariel.discord_bot"
    ) in errors


def test_validate_agency_daemon_posture_accepts_canonical_service() -> None:
    assert (
        validate_agency_daemon_posture(
            active_state="active",
            enabled_state="enabled",
            socket_is_socket=True,
            unit_properties=_canonical_agency_properties(),
        )
        == []
    )


def test_validate_agency_daemon_posture_reports_drift() -> None:
    errors = validate_agency_daemon_posture(
        active_state="inactive",
        enabled_state="disabled",
        socket_is_socket=False,
        unit_properties={
            **_canonical_agency_properties(),
            "Group": "niels",
            "Environment": "AGENCY_DATA_DIR=/tmp/agency",
            "ExecStart": "/tmp/agency daemon",
        },
    )

    assert "agency-daemon active state is 'inactive', expected 'active'" in errors
    assert "agency-daemon enabled state is 'disabled', expected 'enabled'" in errors
    assert f"{REQUIRED_AGENCY_SOCKET_PATH} is not a Unix socket" in errors
    assert "agency-daemon Group is 'niels', expected 'ariel'" in errors
    assert "agency-daemon Environment does not include HOME=/var/lib/agency" in errors
    assert (
        "agency-daemon ExecStart does not include /opt/agency/bin/agency daemon start --foreground"
    ) in errors


def test_validate_api_listener_posture_accepts_loopback_only() -> None:
    assert (
        validate_api_listener_posture(
            listener_addresses=["127.0.0.1", "::1"],
            expected_port=8000,
        )
        == []
    )


def test_validate_api_listener_posture_reports_public_listener() -> None:
    errors = validate_api_listener_posture(
        listener_addresses=["127.0.0.1", "0.0.0.0", "::"],
        expected_port=8000,
    )

    assert "Ariel API listens on non-loopback address 0.0.0.0:8000" in errors
    assert "Ariel API listens on non-loopback address :::8000" in errors


def test_validate_api_listener_posture_reports_missing_listener() -> None:
    assert validate_api_listener_posture(listener_addresses=[], expected_port=8000) == [
        "Ariel API has no listening socket on port 8000"
    ]


def test_validate_caddy_service_posture_accepts_enabled_service_with_public_ports() -> None:
    assert (
        validate_caddy_service_posture(
            active_state="active",
            enabled_state="enabled",
            listener_addresses_by_port={80: ["0.0.0.0"], 443: ["::"]},
        )
        == []
    )


def test_validate_caddy_service_posture_reports_service_and_listener_drift() -> None:
    errors = validate_caddy_service_posture(
        active_state="inactive",
        enabled_state="disabled",
        listener_addresses_by_port={80: ["0.0.0.0"], 443: []},
    )

    assert f"{CADDY_SERVICE} active state is 'inactive', expected 'active'" in errors
    assert f"{CADDY_SERVICE} enabled state is 'disabled', expected 'enabled'" in errors
    assert f"{CADDY_SERVICE} has no listening socket on port 443" in errors


def test_validate_caddy_config_posture_accepts_checked_in_config() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")

    assert (
        validate_caddy_config_posture(
            config_text=text,
            expected_config_text=text,
            config_is_valid=True,
        )
        == []
    )


def test_validate_caddy_config_posture_reports_stale_or_unvalidated_config() -> None:
    stale_text = """
{
	servers {
		trusted_proxies static private_ranges
	}
}

ariel.example.com {
	handle /v1/providers/google/events {
		reverse_proxy 127.0.0.1:8000
	}

	handle /v1/connectors/google/callback {
		reverse_proxy 127.0.0.1:8000
	}

	handle {
		respond 404
	}
}
"""

    errors = validate_caddy_config_posture(
        config_text=stale_text,
        expected_config_text=CADDYFILE.read_text(encoding="utf-8"),
        config_is_valid=False,
    )

    assert f"Caddyfile {REQUIRED_CADDYFILE_PATH} failed caddy validate" in errors
    assert (
        f"Caddyfile {REQUIRED_CADDYFILE_PATH} does not match checked-in deploy/caddy/Caddyfile"
    ) in errors
    assert any("public proxy routes are []" in error for error in errors)
    assert any("path-only reverse proxy handles" in error for error in errors)
    assert f"Caddyfile {REQUIRED_CADDYFILE_PATH} still contains trusted_proxies" in errors


def test_required_caddy_and_firewall_contract_tracks_public_ingress_only() -> None:
    assert REQUIRED_CADDY_PUBLIC_PROXY_ROUTES == {
        ("GET", "/v1/connectors/google/callback"),
        ("POST", "/v1/providers/google/events"),
    }
    assert REQUIRED_PUBLIC_FIREWALL_PORTS == (80, 443)


def test_validate_ufw_firewall_posture_accepts_active_rules_only() -> None:
    active_status = """
Status: active

To                         Action      From
--                         ------      ----
80/tcp                     ALLOW       Anywhere
443/tcp                    ALLOW       Anywhere
80/tcp (v6)                ALLOW       Anywhere (v6)
443/tcp (v6)               ALLOW       Anywhere (v6)
"""

    assert validate_ufw_firewall_posture(status_text=active_status) == []


def test_validate_ufw_firewall_posture_rejects_inactive_or_unavailable_ufw() -> None:
    assert validate_ufw_firewall_posture(status_text="Status: inactive") == [
        "ufw is inactive; firewall posture was not verified"
    ]
    assert validate_ufw_firewall_posture(status_text=None) == [
        "ufw status is unavailable; firewall posture was not verified"
    ]


def test_validate_ufw_firewall_posture_reports_missing_required_port() -> None:
    errors = validate_ufw_firewall_posture(
        status_text="""
Status: active

To                         Action      From
--                         ------      ----
80/tcp                     ALLOW       Anywhere
"""
    )

    assert errors == ["ufw does not allow 443/tcp"]


def test_parse_environment_file_reads_simple_systemd_environment_values() -> None:
    assert parse_environment_file(
        """
        # ignored
        ARIEL_OPENAI_API_KEY=sk-test
        export ARIEL_ANTHROPIC_API_KEY='anthropic-test'
        ARIEL_GOOGLE_API_KEY="google-test"
        """
    ) == {
        "ARIEL_OPENAI_API_KEY": "sk-test",
        "ARIEL_ANTHROPIC_API_KEY": "anthropic-test",
        "ARIEL_GOOGLE_API_KEY": "google-test",
    }


def test_parse_environment_file_rejects_malformed_lines() -> None:
    with pytest.raises(ValueError, match="line 3 is not KEY=VALUE"):
        parse_environment_file(
            """
            ARIEL_OPENAI_API_KEY=sk-test
            malformed
            """
        )


def test_unknown_environment_names_allows_local_helper_env_only_for_local_audit() -> None:
    assert "ARIEL_DB_CONTAINER_NAME" in known_environment_names()
    assert "ARIEL_ENV_FILE" in known_environment_names()
    assert unknown_environment_names(
        {
            "ARIEL_DATABASE_URL": "postgresql+psycopg://localhost/ariel",
            "ARIEL_DB_CONTAINER_NAME": "ariel-postgres-dev",
            "ARIEL_ENV_FILE": ".env.dev",
            "ARIEL_STALE_SECRET": "never-print-me",
        }
    ) == ("ARIEL_STALE_SECRET",)


def test_production_environment_names_exclude_local_selectors_and_dev_helpers() -> None:
    names = production_environment_names()

    assert "ARIEL_DATABASE_URL" in names
    assert "ARIEL_ENV_FILE" not in names
    assert "ARIEL_DB_CONTAINER_NAME" not in names
    assert "ARIEL_DB_DOCKER_IMAGE" not in names
    assert "ARIEL_DB_VOLUME_NAME" not in names


def test_redacted_local_environment_audit_reports_names_not_values() -> None:
    values = _canonical_production_env()
    del values["ARIEL_OPENROUTER_API_KEY"]
    values["ARIEL_LOCAL_AUTH_TOKEN"] = "leaked-secret-value"
    values["ARIEL_STALE_SECRET"] = "never-print-me"

    audit = redacted_local_environment_audit(values=values)

    assert not audit.settings_valid
    assert audit.missing_required_env_vars == ("ARIEL_OPENROUTER_API_KEY",)
    assert audit.unknown_env_vars == ("ARIEL_STALE_SECRET",)
    serialized = " ".join(
        (
            *audit.settings_errors,
            *audit.unknown_env_vars,
            *audit.missing_required_env_vars,
            *audit.present_required_env_vars,
        )
    )
    assert "leaked-secret-value" not in serialized
    assert "never-print-me" not in serialized


def test_redacted_environment_audit_accepts_production_allow_list() -> None:
    values = _canonical_production_env()
    for env_name in DEV_DB_ENV_VARS:
        values[env_name] = "dev-helper"
    values["ARIEL_ENV_FILE"] = ".env.local"

    audit = redacted_local_environment_audit(
        values=values,
        required_env_vars=REQUIRED_PRODUCTION_ENV_VARS,
        allowed_env_names=production_environment_names(),
    )

    assert audit.unknown_env_vars == tuple(sorted((*DEV_DB_ENV_VARS, "ARIEL_ENV_FILE")))


def test_validate_required_environment_values_reports_missing_and_placeholders() -> None:
    errors = validate_required_environment_values(
        values={
            "ARIEL_OPENAI_API_KEY": "sk-test",
            "ARIEL_ANTHROPIC_API_KEY": "<anthropic-api-key>",
            "ARIEL_GOOGLE_API_KEY": "",
        },
        required_env_vars=(
            "ARIEL_OPENAI_API_KEY",
            "ARIEL_ANTHROPIC_API_KEY",
            "ARIEL_GOOGLE_API_KEY",
            "ARIEL_OPENROUTER_API_KEY",
        ),
        source_label="/etc/ariel/ariel.env",
    )

    assert errors == [
        "/etc/ariel/ariel.env ARIEL_ANTHROPIC_API_KEY is blank or a placeholder",
        "/etc/ariel/ariel.env ARIEL_GOOGLE_API_KEY is blank or a placeholder",
        "/etc/ariel/ariel.env missing required environment variable ARIEL_OPENROUTER_API_KEY",
    ]


def test_required_production_env_vars_include_runbook_operational_defaults() -> None:
    assert {
        "ARIEL_MODEL_TIMEOUT_SECONDS",
        "ARIEL_MEMORY_EMBEDDING_DIMENSIONS",
        "ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS",
        "ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS",
        "ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS",
        "ARIEL_AGENCY_TIMEOUT_SECONDS",
        "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS",
        "ARIEL_WORKER_POLL_SECONDS",
        "ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS",
    }.issubset(REQUIRED_PRODUCTION_ENV_VARS)


def test_runbook_documents_pubsub_as_canonical_production_requirement() -> None:
    runbook = (ROOT / "docs/production-runbook.md").read_text(encoding="utf-8")

    assert "Required Google Workspace push settings:" in runbook
    assert "when Gmail/Calendar push is enabled" not in runbook
    assert "production posture requires all three" in runbook


def test_validate_production_environment_settings_accepts_canonical_env() -> None:
    settings, errors = validate_production_environment_settings(
        values=_canonical_production_env(),
        source_label="/etc/ariel/ariel.env",
    )

    assert errors == []
    assert settings is not None
    assert settings.deployment_mode == "production"
    assert settings.attachment_blob_store_path == REQUIRED_ATTACHMENT_BLOB_STORE_PATH


def test_validate_production_environment_settings_rejects_unknown_and_missing_keys() -> None:
    values = _canonical_production_env()
    values["ARIEL_MODEL_NAME"] = "old-env-override"
    for env_name in DEV_DB_ENV_VARS:
        values[env_name] = "dev-helper"
    del values["ARIEL_OPENROUTER_API_KEY"]

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert ("/etc/ariel/ariel.env contains unknown environment variable ARIEL_MODEL_NAME") in errors
    for env_name in DEV_DB_ENV_VARS:
        assert f"/etc/ariel/ariel.env contains unknown environment variable {env_name}" in errors
    assert (
        "/etc/ariel/ariel.env missing required environment variable ARIEL_OPENROUTER_API_KEY"
    ) in errors


def test_redacted_env_file_audit_is_file_only_and_production_strict(tmp_path: Path) -> None:
    values = _canonical_production_env()
    del values["ARIEL_OPENROUTER_API_KEY"]
    del values["ARIEL_GOOGLE_PUBSUB_TOPIC"]
    for env_name in DEV_DB_ENV_VARS:
        values[env_name] = "dev-helper"
    env_file = tmp_path / "ariel.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n",
        encoding="utf-8",
    )
    process_env = {
        **os.environ,
        "ARIEL_OPENROUTER_API_KEY": "shell-openrouter-key",
        "ARIEL_GOOGLE_PUBSUB_TOPIC": "projects/ariel-prod/topics/from-shell",
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(VERIFY_PRODUCTION_POSTURE),
            "--redacted-env-audit",
            "--env-file",
            str(env_file),
        ],
        check=False,
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 1
    assert "- required production env vars missing or placeholder:" in completed.stdout
    assert "ARIEL_OPENROUTER_API_KEY" in completed.stdout
    assert "ARIEL_GOOGLE_PUBSUB_TOPIC" in completed.stdout
    for env_name in DEV_DB_ENV_VARS:
        assert env_name in completed.stdout
        assert f"{env_file} contains unknown environment variable {env_name}" in completed.stdout
    assert "shell-openrouter-key" not in completed.stdout
    assert "from-shell" not in completed.stdout
    assert "Traceback" not in completed.stderr


def test_validate_production_environment_settings_requires_pubsub_env() -> None:
    values = _canonical_production_env()
    del values["ARIEL_GOOGLE_PUBSUB_TOPIC"]
    del values["ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION"]
    del values["ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH"]

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert (
        "/etc/ariel/ariel.env missing required environment variable ARIEL_GOOGLE_PUBSUB_TOPIC"
    ) in errors
    assert (
        "/etc/ariel/ariel.env missing required environment variable "
        "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION"
    ) in errors
    assert (
        "/etc/ariel/ariel.env missing required environment variable "
        "ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH"
    ) in errors


def test_validate_production_environment_settings_requires_reconcile_interval() -> None:
    values = _canonical_production_env()
    del values["ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS"]

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert (
        "/etc/ariel/ariel.env missing required environment variable "
        "ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS"
    ) in errors


def test_validate_production_environment_settings_rejects_subminute_reconcile_interval() -> None:
    values = _canonical_production_env()
    values["ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS"] = str(
        MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS - 1
    )

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert (
        "/etc/ariel/ariel.env ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS is "
        f"{MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS - 1!r}, expected at least "
        f"{MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS}"
    ) in errors


def test_validate_production_environment_settings_rejects_dev_database_placeholders() -> None:
    values = _canonical_production_env()
    values["ARIEL_DATABASE_URL"] = "postgresql+psycopg://ariel:change-me-dev@127.0.0.1:5432/ariel"

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert "/etc/ariel/ariel.env ARIEL_DATABASE_URL is blank or a placeholder" in errors


def test_validate_production_environment_settings_requires_canonical_database_url() -> None:
    values = _canonical_production_env()
    values["ARIEL_DATABASE_URL"] = "postgresql+psycopg://dev:password@127.0.0.1:5435/ariel_dev"

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert "/etc/ariel/ariel.env ARIEL_DATABASE_URL port is 5435, expected 5432" in errors
    assert ("/etc/ariel/ariel.env ARIEL_DATABASE_URL username is 'dev', expected 'ariel'") in errors
    assert (
        "/etc/ariel/ariel.env ARIEL_DATABASE_URL database is 'ariel_dev', expected 'ariel'"
    ) in errors


def test_validate_production_environment_settings_rejects_non_loopback_database_host() -> None:
    values = _canonical_production_env()
    values["ARIEL_DATABASE_URL"] = "postgresql+psycopg://ariel:password@10.0.0.5:5432/ariel"

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert (
        "/etc/ariel/ariel.env ARIEL_DATABASE_URL host is '10.0.0.5', expected '127.0.0.1'"
    ) in errors


def test_validate_production_environment_settings_uses_app_settings_validation() -> None:
    values = _canonical_production_env()
    values["ARIEL_LOCAL_AUTH_TOKEN"] = "weak-token"

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert any("invalid local_auth_token" in error for error in errors)
    assert any("at least 32 URL-safe" in error for error in errors)


def test_validate_production_environment_settings_enforces_canonical_values() -> None:
    values = _canonical_production_env()
    values["ARIEL_AGENCY_SOCKET_PATH"] = "/tmp/agency.sock"
    values["ARIEL_AGENCY_ALLOWED_REPO_ROOTS"] = "/opt/ariel"
    values["ARIEL_AGENCY_DEFAULT_RUNNER"] = "claude-code"
    values["ARIEL_ATTACHMENT_BLOB_STORE_PATH"] = ".ariel/attachment-blobs"

    _settings, errors = validate_production_environment_settings(
        values=values,
        source_label="/etc/ariel/ariel.env",
    )

    assert any("ARIEL_AGENCY_SOCKET_PATH" in error for error in errors)
    assert any("ARIEL_AGENCY_ALLOWED_REPO_ROOTS" in error for error in errors)
    assert any("ARIEL_AGENCY_DEFAULT_RUNNER" in error for error in errors)
    assert any("ARIEL_ATTACHMENT_BLOB_STORE_PATH" in error for error in errors)
