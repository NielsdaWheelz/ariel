#!/usr/bin/env python3
from __future__ import annotations

import argparse
import grp
import json
from pathlib import Path
import pwd
import stat
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from ariel.agency_daemon import AgencyDaemonClient, AgencyDaemonError
from ariel.dev_db import load_local_env
from ariel.production_posture import (
    AGENCY_DAEMON_SERVICE,
    AGENCY_INSTALL_ROOT,
    AGENCY_STATE_ROOT,
    ARIEL_INSTALL_ROOT,
    ARIEL_STATE_ROOT,
    ARIEL_SYSTEMD_SERVICES,
    CADDY_SERVICE,
    POSTGRESQL_SERVICE,
    REQUIRED_AGENCY_BINARY_PATH,
    REQUIRED_AGENCY_SOCKET_PATH,
    REQUIRED_CADDYFILE_PATH,
    REQUIRED_ENVIRONMENT_FILE,
    REQUIRED_PUBLIC_FIREWALL_PORTS,
    REQUIRED_PRODUCTION_ENV_VARS,
    REQUIRED_RUNSC_PATH,
    RedactedEnvironmentAudit,
    production_environment_names,
    validate_agency_daemon_posture,
    validate_api_listener_posture,
    validate_caddy_config_posture,
    validate_caddy_service_posture,
    validate_production_environment_settings,
    validate_production_service_posture,
    validate_ufw_firewall_posture,
    parse_environment_file,
    redacted_local_environment_audit,
)
from ariel.config import AppSettings
from ariel.models import required_model_provider_env_vars


_UNIT_PROPERTIES = (
    "CapabilityBoundingSet",
    "Environment",
    "User",
    "Group",
    "WorkingDirectory",
    "EnvironmentFiles",
    "ExecStart",
    "Restart",
    "RestartUSec",
    "NoNewPrivileges",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectSystem",
    "ProtectHome",
    "ProtectControlGroups",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "RestrictSUIDSGID",
    "LockPersonality",
    "StateDirectory",
    "ReadWritePaths",
    "SystemCallArchitectures",
)


def _run_text(args: list[str], *, check: bool = True) -> str:
    completed = subprocess.run(args, check=check, text=True, capture_output=True)
    return completed.stdout


def _systemctl_lines(command: str, services: tuple[str, ...]) -> dict[str, str]:
    output = _run_text(["systemctl", command, *services], check=False)
    lines = output.splitlines()
    return {
        service: lines[index].strip() if index < len(lines) else ""
        for index, service in enumerate(services)
    }


def _unit_properties(service: str) -> dict[str, str]:
    output = _run_text(
        [
            "systemctl",
            "show",
            *(f"-p{name}" for name in _UNIT_PROPERTIES),
            service,
            "--no-pager",
        ]
    )
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", maxsplit=1)
        properties[name] = value
    return properties


def _health_payload(url: str) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"health endpoint check failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("health endpoint returned a non-object payload")
    return payload


def _path_is_socket(path: str) -> bool:
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _agency_health_error(socket_path: str) -> str | None:
    try:
        AgencyDaemonClient(socket_path=socket_path, timeout_seconds=5.0).health()
    except AgencyDaemonError as exc:
        return f"agency daemon health check failed: {exc}"
    return None


def _owner_group(path: Path) -> tuple[str, str]:
    metadata = path.stat()
    try:
        owner = pwd.getpwuid(metadata.st_uid).pw_name
    except KeyError:
        owner = str(metadata.st_uid)
    try:
        group = grp.getgrgid(metadata.st_gid).gr_name
    except KeyError:
        group = str(metadata.st_gid)
    return owner, group


def _listener_addresses_for_port(port: int) -> list[str]:
    output = _run_text(["ss", "-ltnH"], check=True)
    addresses: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        host, separator, raw_port = local.rpartition(":")
        if not separator or raw_port != str(port):
            continue
        addresses.append(host.strip("[]").split("%", maxsplit=1)[0])
    return addresses


def _health_url_port(url: str) -> int:
    parsed = urlparse(url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_text_if_available(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _caddy_config_is_valid(path: str) -> bool:
    try:
        completed = subprocess.run(
            ["caddy", "validate", "--config", path],
            check=False,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return completed.returncode == 0


def _ufw_status_text() -> str | None:
    try:
        completed = subprocess.run(["ufw", "status"], check=False, text=True, capture_output=True)
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _required_artifact_errors() -> tuple[AppSettings | None, list[str]]:
    errors: list[str] = []
    production_settings: AppSettings | None = None
    required_directories = {
        ARIEL_INSTALL_ROOT: "Ariel install root",
        ARIEL_STATE_ROOT: "Ariel state root",
        AGENCY_INSTALL_ROOT: "Agency install root",
        AGENCY_STATE_ROOT: "Agency state root",
    }
    for path, label in required_directories.items():
        candidate = Path(path)
        if not candidate.is_dir():
            errors.append(f"{label} {path} is not a directory")

    required_executables = {
        REQUIRED_AGENCY_BINARY_PATH: "Agency binary",
        REQUIRED_RUNSC_PATH: "runsc binary",
    }
    for path, label in required_executables.items():
        candidate = Path(path)
        if not candidate.is_file():
            errors.append(f"{label} {path} is not a regular file")
            continue
        if candidate.stat().st_mode & 0o111 == 0:
            errors.append(f"{label} {path} is not executable")

    env_file = Path(REQUIRED_ENVIRONMENT_FILE)
    if not env_file.is_file():
        errors.append(f"environment file {REQUIRED_ENVIRONMENT_FILE} is not a regular file")
    else:
        env_metadata = env_file.stat()
        env_mode = stat.S_IMODE(env_metadata.st_mode)
        if env_mode != 0o640:
            errors.append(
                f"environment file {REQUIRED_ENVIRONMENT_FILE} mode is "
                f"{env_mode:04o}, expected 0640"
            )
        env_owner, env_group = _owner_group(env_file)
        if (env_owner, env_group) != ("root", "ariel"):
            errors.append(
                f"environment file {REQUIRED_ENVIRONMENT_FILE} owner is "
                f"{env_owner}:{env_group}, expected root:ariel"
            )
        try:
            env_values = parse_environment_file(env_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"environment file {REQUIRED_ENVIRONMENT_FILE} parse error: {exc}")
            env_values = None
        if env_values is not None:
            production_settings, env_errors = validate_production_environment_settings(
                values=env_values,
                source_label=f"environment file {REQUIRED_ENVIRONMENT_FILE}",
            )
            errors.extend(env_errors)

    agency_binary = Path(REQUIRED_AGENCY_BINARY_PATH)
    if agency_binary.exists():
        owner, _group = _owner_group(agency_binary)
        if owner != "root":
            errors.append(
                f"Agency binary {REQUIRED_AGENCY_BINARY_PATH} owner is {owner}, expected root"
            )
    return production_settings, errors


def _schema_readiness_errors(settings: AppSettings) -> list[str]:
    from sqlalchemy import create_engine

    from ariel.db import schema_readiness_issues

    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
    try:
        schema_issues = schema_readiness_issues(engine)
    finally:
        engine.dispose()
    if schema_issues:
        return [f"schema readiness issues: {schema_issues}"]
    return []


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the live Ariel host matches the production service posture."
    )
    parser.add_argument(
        "--redacted-env-audit",
        action="store_true",
        help="Audit the active local env stack without printing secret values.",
    )
    parser.add_argument(
        "--env-file",
        help=(
            "Env file to audit with --redacted-env-audit. Relative paths are "
            "resolved from the repo root; production should use /etc/ariel/ariel.env."
        ),
    )
    parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:8000/v1/health",
        help="Loopback health URL to check.",
    )
    parser.add_argument(
        "--check-db-schema",
        action="store_true",
        help="Also load Ariel settings and query the database schema readiness checks.",
    )
    return parser


def _redacted_env_file_path(env_file: str) -> Path:
    path = Path(env_file)
    if not path.is_absolute():
        path = _repo_root() / path
    if not path.exists():
        msg = f"--env-file points to missing env file: {path}"
        raise FileNotFoundError(msg)
    if not path.is_file():
        msg = f"--env-file must point to a file: {path}"
        raise OSError(msg)
    return path


def _print_redacted_env_audit(
    *,
    audit: RedactedEnvironmentAudit,
    required_label: str,
    settings_valid: bool,
    settings_errors: tuple[str, ...],
) -> None:
    print(f"- settings valid: {settings_valid}")
    print(f"- known env vars present: {audit.present_known_env_count}")
    print(
        f"- required {required_label} present: "
        + (", ".join(audit.present_required_env_vars) or "none")
    )
    print(
        f"- required {required_label} missing or placeholder: "
        + (", ".join(audit.missing_required_env_vars) or "none")
    )
    print("- unknown ARIEL_* names: " + (", ".join(audit.unknown_env_vars) or "none"))
    if settings_errors:
        print("- settings errors:")
        for error in settings_errors:
            print(f"  - {error}")


def _run_redacted_env_audit(env_file: str | None) -> int:
    print("redacted env audit:")
    try:
        if env_file is None:
            values = load_local_env(_repo_root())
            audit = redacted_local_environment_audit(values=values)
            _print_redacted_env_audit(
                audit=audit,
                required_label="model provider keys",
                settings_valid=audit.settings_valid,
                settings_errors=audit.settings_errors,
            )
            return int(
                bool(
                    audit.settings_errors
                    or audit.unknown_env_vars
                    or audit.missing_required_env_vars
                )
            )

        env_path = _redacted_env_file_path(env_file)
        values = parse_environment_file(env_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"- error: {exc}")
        return 1

    required_env_vars = REQUIRED_PRODUCTION_ENV_VARS + required_model_provider_env_vars()
    audit = redacted_local_environment_audit(
        values=values,
        required_env_vars=required_env_vars,
        allowed_env_names=production_environment_names(),
    )
    _settings, production_errors = validate_production_environment_settings(
        values=values,
        source_label=str(env_path),
    )
    settings_errors = tuple(production_errors)
    _print_redacted_env_audit(
        audit=audit,
        required_label="production env vars",
        settings_valid=not settings_errors,
        settings_errors=settings_errors,
    )

    if settings_errors or audit.unknown_env_vars or audit.missing_required_env_vars:
        return 1
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.redacted_env_audit:
        return _run_redacted_env_audit(args.env_file)

    errors: list[str] = []
    caddy_active_state: str | None = None
    caddy_enabled_state: str | None = None
    try:
        errors.extend(
            validate_production_service_posture(
                active_states=_systemctl_lines("is-active", ARIEL_SYSTEMD_SERVICES),
                enabled_states=_systemctl_lines("is-enabled", ARIEL_SYSTEMD_SERVICES),
                unit_properties={
                    service: _unit_properties(service) for service in ARIEL_SYSTEMD_SERVICES
                },
            )
        )
        agency_active = _systemctl_lines("is-active", (AGENCY_DAEMON_SERVICE,))
        agency_enabled = _systemctl_lines("is-enabled", (AGENCY_DAEMON_SERVICE,))
        agency_socket_is_socket = _path_is_socket(REQUIRED_AGENCY_SOCKET_PATH)
        errors.extend(
            validate_agency_daemon_posture(
                active_state=agency_active.get(AGENCY_DAEMON_SERVICE),
                enabled_state=agency_enabled.get(AGENCY_DAEMON_SERVICE),
                socket_is_socket=agency_socket_is_socket,
                unit_properties=_unit_properties(AGENCY_DAEMON_SERVICE),
            )
        )
        postgresql_active = _systemctl_lines("is-active", (POSTGRESQL_SERVICE,))
        if postgresql_active.get(POSTGRESQL_SERVICE) != "active":
            errors.append(
                f"{POSTGRESQL_SERVICE} active state is "
                f"{postgresql_active.get(POSTGRESQL_SERVICE)!r}, expected 'active'"
            )
        caddy_active = _systemctl_lines("is-active", (CADDY_SERVICE,))
        caddy_enabled = _systemctl_lines("is-enabled", (CADDY_SERVICE,))
        caddy_active_state = caddy_active.get(CADDY_SERVICE)
        caddy_enabled_state = caddy_enabled.get(CADDY_SERVICE)
        if agency_socket_is_socket:
            agency_health_error = _agency_health_error(REQUIRED_AGENCY_SOCKET_PATH)
            if agency_health_error is not None:
                errors.append(agency_health_error)
    except subprocess.CalledProcessError as exc:
        errors.append(f"systemctl check failed: {exc}")

    production_settings, artifact_errors = _required_artifact_errors()
    errors.extend(artifact_errors)

    try:
        payload = _health_payload(str(args.health_url))
        if payload.get("ok") is not True:
            errors.append("health endpoint is not ok")
    except RuntimeError as exc:
        errors.append(str(exc))

    caddy_listener_addresses: dict[int, list[str]] | None = None
    try:
        caddy_listener_addresses = {
            port: _listener_addresses_for_port(port) for port in REQUIRED_PUBLIC_FIREWALL_PORTS
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"caddy listener check failed: {exc}")
    errors.extend(
        validate_caddy_service_posture(
            active_state=caddy_active_state,
            enabled_state=caddy_enabled_state,
            listener_addresses_by_port=caddy_listener_addresses,
        )
    )

    live_caddyfile = _read_text_if_available(Path(REQUIRED_CADDYFILE_PATH))
    expected_caddyfile_path = _repo_root() / "deploy/caddy/Caddyfile"
    expected_caddyfile = _read_text_if_available(expected_caddyfile_path)
    if expected_caddyfile is None:
        errors.append("checked-in deploy/caddy/Caddyfile is not readable")
    errors.extend(
        validate_caddy_config_posture(
            config_text=live_caddyfile,
            expected_config_text=expected_caddyfile,
            config_is_valid=_caddy_config_is_valid(REQUIRED_CADDYFILE_PATH),
        )
    )
    errors.extend(validate_ufw_firewall_posture(status_text=_ufw_status_text()))

    try:
        health_port = _health_url_port(str(args.health_url))
        errors.extend(
            validate_api_listener_posture(
                listener_addresses=_listener_addresses_for_port(health_port),
                expected_port=health_port,
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"listener check failed: {exc}")

    if args.check_db_schema:
        if production_settings is None:
            errors.append("database schema check skipped because production env did not validate")
        else:
            errors.extend(_schema_readiness_errors(production_settings))

    if errors:
        print("production posture check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("production posture check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
