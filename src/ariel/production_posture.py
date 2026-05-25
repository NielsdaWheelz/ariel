from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Mapping
from ipaddress import ip_address
from urllib.parse import unquote, urlparse

from pydantic import ValidationError

from ariel.config import AppSettings, ENV_FILE_SELECTOR_ENV_VAR
from ariel.dev_db import DEV_DB_ENV_VARS
from ariel.models import required_model_provider_env_vars


ARIEL_SYSTEMD_SERVICES: tuple[str, ...] = (
    "ariel-api",
    "ariel-worker",
    "ariel-pubsub",
    "ariel-discord",
)
AGENCY_DAEMON_SERVICE = "agency-daemon"
POSTGRESQL_SERVICE = "postgresql"
CADDY_SERVICE = "caddy"
ARIEL_INSTALL_ROOT = "/opt/ariel"
ARIEL_STATE_ROOT = "/var/lib/ariel"
AGENCY_INSTALL_ROOT = "/opt/agency"
AGENCY_STATE_ROOT = "/var/lib/agency"
AGENCY_DATA_DIR = AGENCY_STATE_ROOT
REQUIRED_AGENCY_BINARY_PATH = f"{AGENCY_INSTALL_ROOT}/bin/agency"
REQUIRED_AGENCY_SOCKET_PATH = f"{AGENCY_DATA_DIR}/agencyd.sock"
REQUIRED_RUNSC_PATH = "/usr/local/bin/runsc"
REQUIRED_ATTACHMENT_BLOB_STORE_PATH = f"{ARIEL_STATE_ROOT}/attachment-blobs"
REQUIRED_CADDYFILE_PATH = "/etc/caddy/Caddyfile"
REQUIRED_CADDY_PUBLIC_PROXY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/connectors/google/callback"),
        ("POST", "/v1/providers/google/events"),
    }
)
REQUIRED_PUBLIC_FIREWALL_PORTS: tuple[int, ...] = (80, 443)

REQUIRED_UNIT_PROPERTIES: dict[str, str] = {
    "User": "ariel",
    "Group": "ariel",
    "WorkingDirectory": ARIEL_INSTALL_ROOT,
    "Restart": "always",
    "RestartUSec": "5s",
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectSystem": "full",
    "ProtectHome": "yes",
    "ProtectControlGroups": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    "CapabilityBoundingSet": "",
    "SystemCallArchitectures": "native",
}

REQUIRED_UNIT_EXECSTART_SUBSTRINGS: dict[str, str] = {
    "ariel-api": f"{ARIEL_INSTALL_ROOT}/.venv/bin/uvicorn ariel.app:create_app --factory",
    "ariel-worker": f"{ARIEL_INSTALL_ROOT}/.venv/bin/python -m ariel.worker",
    "ariel-pubsub": f"{ARIEL_INSTALL_ROOT}/.venv/bin/python -m ariel.pubsub_subscriber",
    "ariel-discord": f"{ARIEL_INSTALL_ROOT}/.venv/bin/python -m ariel.discord_bot",
}

REQUIRED_ENVIRONMENT_FILE = "/etc/ariel/ariel.env"

REQUIRED_AGENCY_UNIT_PROPERTIES: dict[str, str] = {
    "User": "ariel",
    "Group": "ariel",
    "WorkingDirectory": AGENCY_INSTALL_ROOT,
    "StateDirectory": "agency",
    "Restart": "always",
    "RestartUSec": "5s",
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectSystem": "full",
    "ProtectHome": "yes",
    "ProtectControlGroups": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    "CapabilityBoundingSet": "",
    "SystemCallArchitectures": "native",
    "ReadWritePaths": AGENCY_STATE_ROOT,
}

REQUIRED_AGENCY_ENVIRONMENT_SUBSTRINGS: tuple[str, ...] = (
    f"AGENCY_DATA_DIR={AGENCY_DATA_DIR}",
    f"HOME={AGENCY_STATE_ROOT}",
    f"CODEX_HOME={AGENCY_STATE_ROOT}/.codex",
)
REQUIRED_AGENCY_EXECSTART_SUBSTRING = f"{REQUIRED_AGENCY_BINARY_PATH} daemon start --foreground"
REQUIRED_PRODUCTION_ENV_VARS: tuple[str, ...] = (
    "ARIEL_DATABASE_URL",
    "ARIEL_DEPLOYMENT_MODE",
    "ARIEL_BIND_HOST",
    "ARIEL_BIND_PORT",
    "ARIEL_LOCAL_AUTH_REQUIRED",
    "ARIEL_LOCAL_AUTH_TOKEN",
    "ARIEL_CONNECTOR_ENCRYPTION_SECRET",
    "ARIEL_CONNECTOR_ENCRYPTION_KEY_VERSION",
    "ARIEL_CONNECTOR_ENCRYPTION_KEYS",
    "ARIEL_PUBLIC_WEBHOOK_BASE_URL",
    "ARIEL_MODEL_REASONING_EFFORT",
    "ARIEL_MODEL_TIMEOUT_SECONDS",
    "ARIEL_MEMORY_EMBEDDING_DIMENSIONS",
    "ARIEL_GOOGLE_OAUTH_CLIENT_ID",
    "ARIEL_GOOGLE_OAUTH_CLIENT_SECRET",
    "ARIEL_GOOGLE_OAUTH_REDIRECT_URI",
    "ARIEL_GOOGLE_OAUTH_STATE_TTL_SECONDS",
    "ARIEL_GOOGLE_OAUTH_TIMEOUT_SECONDS",
    "ARIEL_GOOGLE_PUBSUB_TOPIC",
    "ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION",
    "ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH",
    "ARIEL_DISCORD_BOT_TOKEN",
    "ARIEL_DISCORD_GUILD_ID",
    "ARIEL_DISCORD_CHANNEL_ID",
    "ARIEL_DISCORD_USER_ID",
    "ARIEL_DISCORD_ARIEL_BASE_URL",
    "ARIEL_DISCORD_NOTIFICATION_TIMEOUT_SECONDS",
    "ARIEL_AGENCY_SOCKET_PATH",
    "ARIEL_AGENCY_ALLOWED_REPO_ROOTS",
    "ARIEL_AGENCY_DEFAULT_BASE_BRANCH",
    "ARIEL_AGENCY_DEFAULT_RUNNER",
    "ARIEL_AGENCY_TIMEOUT_SECONDS",
    "ARIEL_AGENCY_EVENT_MAX_SKEW_SECONDS",
    "ARIEL_ATTACHMENT_BLOB_STORE_PATH",
    "ARIEL_WORKER_POLL_SECONDS",
    "ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS",
)
MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class RedactedEnvironmentAudit:
    settings_valid: bool
    settings_errors: tuple[str, ...]
    unknown_env_vars: tuple[str, ...]
    missing_required_env_vars: tuple[str, ...]
    present_required_env_vars: tuple[str, ...]
    present_known_env_count: int


def parse_environment_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator:
            msg = f"line {line_number} is not KEY=VALUE"
            raise ValueError(msg)
        key = key.strip()
        if not key:
            msg = f"line {line_number} has a blank key"
            raise ValueError(msg)
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _is_placeholder_env_value(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    lowered = normalized.lower()
    return (
        lowered in {"your_real_key", "changeme", "change_me", "replace_me"}
        or any(
            fragment in lowered
            for fragment in (
                "your_real_key",
                "change-me",
                "change_me",
                "changeme",
                "replace-me",
                "replace_me",
                "dev-only-password",
                "dev-local-connector-secret",
            )
        )
        or "placeholder" in lowered
        or (normalized.startswith("<") and normalized.endswith(">"))
    )


def validate_required_environment_values(
    *,
    values: Mapping[str, str],
    required_env_vars: tuple[str, ...],
    source_label: str,
) -> list[str]:
    errors: list[str] = []
    for env_name in required_env_vars:
        value = values.get(env_name)
        if value is None:
            errors.append(f"{source_label} missing required environment variable {env_name}")
            continue
        if _is_placeholder_env_value(value):
            errors.append(f"{source_label} {env_name} is blank or a placeholder")
    return errors


def _settings_env_names() -> set[str]:
    return {f"ARIEL_{name.upper()}" for name in AppSettings.model_fields}


def known_environment_names() -> frozenset[str]:
    return frozenset(_settings_env_names() | DEV_DB_ENV_VARS | {ENV_FILE_SELECTOR_ENV_VAR})


def production_environment_names() -> frozenset[str]:
    return frozenset(_settings_env_names())


def unknown_environment_names(
    values: Mapping[str, str],
    *,
    allowed_env_names: frozenset[str] | set[str] | None = None,
) -> tuple[str, ...]:
    allowed = known_environment_names() if allowed_env_names is None else allowed_env_names
    return tuple(
        sorted(
            env_name
            for env_name in values
            if env_name.startswith("ARIEL_") and env_name not in allowed
        )
    )


def _settings_data_from_env_values(values: Mapping[str, str]) -> dict[str, str]:
    allowed_env_names = _settings_env_names()
    return {
        env_name.removeprefix("ARIEL_").lower(): value
        for env_name, value in values.items()
        if env_name in allowed_env_names
    }


def _validation_error_messages(*, exc: ValidationError, source_label: str) -> list[str]:
    messages: list[str] = []
    for error in exc.errors(include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        messages.append(f"{source_label} invalid {loc}: {error.get('msg', 'validation failed')}")
    return messages


def redacted_local_environment_audit(
    *,
    values: Mapping[str, str],
    required_env_vars: tuple[str, ...] | None = None,
    allowed_env_names: frozenset[str] | set[str] | None = None,
) -> RedactedEnvironmentAudit:
    required = (
        required_model_provider_env_vars() if required_env_vars is None else required_env_vars
    )
    missing_required = tuple(
        env_name
        for env_name in required
        if values.get(env_name) is None or _is_placeholder_env_value(values[env_name])
    )
    present_required = tuple(env_name for env_name in required if env_name not in missing_required)

    settings_errors: tuple[str, ...] = ()
    try:
        AppSettings.model_validate(_settings_data_from_env_values(values))
    except ValidationError as exc:
        settings_errors = tuple(
            _validation_error_messages(exc=exc, source_label="active env stack")
        )

    return RedactedEnvironmentAudit(
        settings_valid=not settings_errors,
        settings_errors=settings_errors,
        unknown_env_vars=unknown_environment_names(values, allowed_env_names=allowed_env_names),
        missing_required_env_vars=missing_required,
        present_required_env_vars=present_required,
        present_known_env_count=sum(
            1
            for env_name in (
                known_environment_names() if allowed_env_names is None else allowed_env_names
            )
            if env_name in values
        ),
    )


def _validate_production_database_url(*, database_url: str, source_label: str) -> list[str]:
    errors: list[str] = []
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        errors.append(
            f"{source_label} ARIEL_DATABASE_URL scheme is {parsed.scheme!r}, "
            "expected 'postgresql+psycopg'"
        )
    if parsed.hostname != "127.0.0.1":
        errors.append(
            f"{source_label} ARIEL_DATABASE_URL host is {parsed.hostname!r}, expected '127.0.0.1'"
        )
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port != 5432:
        errors.append(f"{source_label} ARIEL_DATABASE_URL port is {port!r}, expected 5432")
    username = unquote(parsed.username or "")
    if username != "ariel":
        errors.append(
            f"{source_label} ARIEL_DATABASE_URL username is {username!r}, expected 'ariel'"
        )
    database_name = unquote(parsed.path.lstrip("/"))
    if database_name != "ariel":
        errors.append(
            f"{source_label} ARIEL_DATABASE_URL database is {database_name!r}, expected 'ariel'"
        )
    return errors


def validate_production_environment_settings(
    *,
    values: Mapping[str, str],
    source_label: str,
) -> tuple[AppSettings | None, list[str]]:
    errors: list[str] = []
    for env_name in unknown_environment_names(
        values, allowed_env_names=production_environment_names()
    ):
        errors.append(f"{source_label} contains unknown environment variable {env_name}")

    errors.extend(
        validate_required_environment_values(
            values=values,
            required_env_vars=REQUIRED_PRODUCTION_ENV_VARS + required_model_provider_env_vars(),
            source_label=source_label,
        )
    )

    settings: AppSettings | None = None
    try:
        settings = AppSettings.model_validate(_settings_data_from_env_values(values))
    except ValidationError as exc:
        errors.extend(_validation_error_messages(exc=exc, source_label=source_label))

    if settings is None:
        return None, errors

    if settings.deployment_mode != "production":
        errors.append(
            f"{source_label} ARIEL_DEPLOYMENT_MODE is {settings.deployment_mode!r}, "
            "expected 'production'"
        )
    if settings.bind_host != "127.0.0.1":
        errors.append(
            f"{source_label} ARIEL_BIND_HOST is {settings.bind_host!r}, expected '127.0.0.1'"
        )
    if settings.bind_port != 8000:
        errors.append(f"{source_label} ARIEL_BIND_PORT is {settings.bind_port!r}, expected 8000")
    errors.extend(
        _validate_production_database_url(
            database_url=settings.database_url,
            source_label=source_label,
        )
    )
    if settings.discord_ariel_base_url != "http://127.0.0.1:8000":
        errors.append(
            f"{source_label} ARIEL_DISCORD_ARIEL_BASE_URL is "
            f"{settings.discord_ariel_base_url!r}, expected 'http://127.0.0.1:8000'"
        )
    if settings.agency_socket_path != REQUIRED_AGENCY_SOCKET_PATH:
        errors.append(
            f"{source_label} ARIEL_AGENCY_SOCKET_PATH is {settings.agency_socket_path!r}, "
            f"expected {REQUIRED_AGENCY_SOCKET_PATH!r}"
        )
    allowed_roots = {
        root.strip() for root in settings.agency_allowed_repo_roots.split(",") if root.strip()
    }
    expected_roots = {ARIEL_INSTALL_ROOT, AGENCY_INSTALL_ROOT}
    if allowed_roots != expected_roots:
        errors.append(
            f"{source_label} ARIEL_AGENCY_ALLOWED_REPO_ROOTS is "
            f"{sorted(allowed_roots)!r}, expected {sorted(expected_roots)!r}"
        )
    if settings.agency_default_base_branch != "main":
        errors.append(
            f"{source_label} ARIEL_AGENCY_DEFAULT_BASE_BRANCH is "
            f"{settings.agency_default_base_branch!r}, expected 'main'"
        )
    if settings.agency_default_runner != "codex":
        errors.append(
            f"{source_label} ARIEL_AGENCY_DEFAULT_RUNNER is "
            f"{settings.agency_default_runner!r}, expected 'codex'"
        )
    if settings.attachment_blob_store_path != REQUIRED_ATTACHMENT_BLOB_STORE_PATH:
        errors.append(
            f"{source_label} ARIEL_ATTACHMENT_BLOB_STORE_PATH is "
            f"{settings.attachment_blob_store_path!r}, "
            f"expected {REQUIRED_ATTACHMENT_BLOB_STORE_PATH!r}"
        )
    if (
        settings.provider_reconcile_sync_interval_seconds
        < MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS
    ):
        errors.append(
            f"{source_label} ARIEL_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS is "
            f"{settings.provider_reconcile_sync_interval_seconds!r}, expected at least "
            f"{MIN_PRODUCTION_PROVIDER_RECONCILE_SYNC_INTERVAL_SECONDS}"
        )
    return settings, errors


def validate_production_service_posture(
    *,
    active_states: Mapping[str, str],
    enabled_states: Mapping[str, str],
    unit_properties: Mapping[str, Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    for service in ARIEL_SYSTEMD_SERVICES:
        active_state = active_states.get(service)
        if active_state != "active":
            errors.append(f"{service} active state is {active_state!r}, expected 'active'")

        enabled_state = enabled_states.get(service)
        if enabled_state != "enabled":
            errors.append(f"{service} enabled state is {enabled_state!r}, expected 'enabled'")

        properties = unit_properties.get(service, {})
        for property_name, expected_value in REQUIRED_UNIT_PROPERTIES.items():
            actual_value = properties.get(property_name)
            if actual_value != expected_value:
                errors.append(
                    f"{service} {property_name} is {actual_value!r}, expected {expected_value!r}"
                )

        environment_files = properties.get("EnvironmentFiles", "")
        if REQUIRED_ENVIRONMENT_FILE not in environment_files:
            errors.append(
                f"{service} EnvironmentFiles does not include {REQUIRED_ENVIRONMENT_FILE}"
            )

        exec_start = properties.get("ExecStart", "")
        exec_start_substring = REQUIRED_UNIT_EXECSTART_SUBSTRINGS[service]
        if exec_start_substring not in exec_start:
            errors.append(f"{service} ExecStart does not include {exec_start_substring}")
    return errors


def validate_agency_daemon_posture(
    *,
    active_state: str | None,
    enabled_state: str | None,
    socket_is_socket: bool,
    unit_properties: Mapping[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if active_state != "active":
        errors.append(
            f"{AGENCY_DAEMON_SERVICE} active state is {active_state!r}, expected 'active'"
        )
    if enabled_state != "enabled":
        errors.append(
            f"{AGENCY_DAEMON_SERVICE} enabled state is {enabled_state!r}, expected 'enabled'"
        )
    if not socket_is_socket:
        errors.append(f"{REQUIRED_AGENCY_SOCKET_PATH} is not a Unix socket")
    if unit_properties is not None:
        for property_name, expected_value in REQUIRED_AGENCY_UNIT_PROPERTIES.items():
            actual_value = unit_properties.get(property_name)
            if actual_value != expected_value:
                errors.append(
                    f"{AGENCY_DAEMON_SERVICE} {property_name} is {actual_value!r}, "
                    f"expected {expected_value!r}"
                )

        environment = unit_properties.get("Environment", "")
        for required_fragment in REQUIRED_AGENCY_ENVIRONMENT_SUBSTRINGS:
            if required_fragment not in environment:
                errors.append(
                    f"{AGENCY_DAEMON_SERVICE} Environment does not include {required_fragment}"
                )

        exec_start = unit_properties.get("ExecStart", "")
        if REQUIRED_AGENCY_EXECSTART_SUBSTRING not in exec_start:
            errors.append(
                f"{AGENCY_DAEMON_SERVICE} ExecStart does not include "
                f"{REQUIRED_AGENCY_EXECSTART_SUBSTRING}"
            )
    return errors


def _format_caddy_routes(routes: set[tuple[str, str]] | frozenset[tuple[str, str]]) -> list[str]:
    return [f"{method} {path}" for method, path in sorted(routes)]


def _caddy_public_proxy_routes(config_text: str) -> set[tuple[str, str]]:
    matcher_routes: dict[str, tuple[str, str]] = {}
    for name, body in re.findall(
        r"^\s*@([A-Za-z0-9_]+)\s+\{\n(?P<body>.*?)^\s*\}",
        config_text,
        re.MULTILINE | re.DOTALL,
    ):
        method_match = re.search(r"^\s*method\s+([A-Z]+)\s*$", body, re.MULTILINE)
        path_match = re.search(r"^\s*path\s+([^\s]+)\s*$", body, re.MULTILINE)
        if method_match is not None and path_match is not None:
            matcher_routes[name] = (method_match.group(1), path_match.group(1))

    proxied_routes: set[tuple[str, str]] = set()
    for name in re.findall(
        r"^\s*handle\s+@([A-Za-z0-9_]+)\s+\{\n\s+reverse_proxy 127\.0\.0\.1:8000\n\s+\}",
        config_text,
        re.MULTILINE,
    ):
        route = matcher_routes.get(name)
        if route is not None:
            proxied_routes.add(route)
    return proxied_routes


def _caddy_path_only_proxy_handles(config_text: str) -> set[str]:
    return set(
        re.findall(
            r"^\s*handle\s+(/[^\s]+)\s+\{\n\s+reverse_proxy 127\.0\.0\.1:8000\n\s+\}",
            config_text,
            re.MULTILINE,
        )
    )


def validate_caddy_config_posture(
    *,
    config_text: str | None,
    expected_config_text: str | None,
    config_is_valid: bool,
    config_path: str = REQUIRED_CADDYFILE_PATH,
) -> list[str]:
    errors: list[str] = []
    if not config_is_valid:
        errors.append(f"Caddyfile {config_path} failed caddy validate")
    if config_text is None:
        errors.append(f"Caddyfile {config_path} is not readable")
        return errors
    if expected_config_text is not None and config_text != expected_config_text:
        errors.append(f"Caddyfile {config_path} does not match checked-in deploy/caddy/Caddyfile")

    actual_routes = _caddy_public_proxy_routes(config_text)
    if actual_routes != REQUIRED_CADDY_PUBLIC_PROXY_ROUTES:
        errors.append(
            f"Caddyfile {config_path} public proxy routes are "
            f"{_format_caddy_routes(actual_routes)!r}, expected "
            f"{_format_caddy_routes(REQUIRED_CADDY_PUBLIC_PROXY_ROUTES)!r}"
        )

    path_only_handles = _caddy_path_only_proxy_handles(config_text)
    if path_only_handles:
        errors.append(
            f"Caddyfile {config_path} has path-only reverse proxy handles without "
            f"method matchers: {sorted(path_only_handles)!r}"
        )

    proxy_count = config_text.count("reverse_proxy 127.0.0.1:8000")
    expected_proxy_count = len(REQUIRED_CADDY_PUBLIC_PROXY_ROUTES)
    if proxy_count != expected_proxy_count:
        errors.append(
            f"Caddyfile {config_path} has {proxy_count} Ariel reverse_proxy directives, "
            f"expected {expected_proxy_count}"
        )

    if "trusted_proxies" in config_text:
        errors.append(f"Caddyfile {config_path} still contains trusted_proxies")
    for query_key in ("code", "state"):
        if f"replace {query_key} REDACTED" not in config_text:
            errors.append(f"Caddyfile {config_path} does not redact query param {query_key}")
    if re.search(r"handle \{\n\s+respond 404\n\s+\}", config_text) is None:
        errors.append(f"Caddyfile {config_path} does not have a default 404 handler")
    return errors


def validate_caddy_service_posture(
    *,
    active_state: str | None,
    enabled_state: str | None,
    listener_addresses_by_port: Mapping[int, list[str]] | None,
) -> list[str]:
    errors: list[str] = []
    if active_state != "active":
        errors.append(f"{CADDY_SERVICE} active state is {active_state!r}, expected 'active'")
    if enabled_state != "enabled":
        errors.append(f"{CADDY_SERVICE} enabled state is {enabled_state!r}, expected 'enabled'")
    if listener_addresses_by_port is not None:
        for port in REQUIRED_PUBLIC_FIREWALL_PORTS:
            if not listener_addresses_by_port.get(port):
                errors.append(f"{CADDY_SERVICE} has no listening socket on port {port}")
    return errors


def validate_ufw_firewall_posture(
    *,
    status_text: str | None,
    required_ports: tuple[int, ...] = REQUIRED_PUBLIC_FIREWALL_PORTS,
) -> list[str]:
    if status_text is None:
        return ["ufw status is unavailable; firewall posture was not verified"]

    normalized_status = status_text.lower()
    if "status: inactive" in normalized_status:
        return ["ufw is inactive; firewall posture was not verified"]

    errors: list[str] = []
    if "status: active" not in normalized_status:
        return ["ufw status is neither active nor inactive; firewall posture was not verified"]

    allowed_lines = [
        line
        for line in status_text.splitlines()
        if "ALLOW" in line and not line.lstrip().startswith("[")
    ]
    for port in required_ports:
        needle = f"{port}/tcp"
        if not any(needle in line for line in allowed_lines):
            errors.append(f"ufw does not allow {needle}")
    return errors


def validate_api_listener_posture(
    *, listener_addresses: list[str], expected_port: int
) -> list[str]:
    errors: list[str] = []
    if not listener_addresses:
        errors.append(f"Ariel API has no listening socket on port {expected_port}")
        return errors
    for address in listener_addresses:
        try:
            parsed_address = ip_address(address)
        except ValueError:
            errors.append(f"Ariel API listener address {address!r} is not an IP address")
            continue
        if not parsed_address.is_loopback:
            errors.append(f"Ariel API listens on non-loopback address {address}:{expected_port}")
    return errors
