#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from ariel.agency_daemon import AgencyDaemonClient, AgencyDaemonError
from ariel.production_posture import (
    AGENCY_DAEMON_SERVICE,
    ARIEL_SYSTEMD_SERVICES,
    REQUIRED_AGENCY_SOCKET_PATH,
    validate_agency_daemon_posture,
    validate_production_service_posture,
)


_UNIT_PROPERTIES = (
    "User",
    "WorkingDirectory",
    "EnvironmentFiles",
    "NoNewPrivileges",
    "ProtectSystem",
    "ProtectHome",
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


def _schema_readiness_errors() -> list[str]:
    from sqlalchemy import create_engine

    from ariel.config import AppSettings
    from ariel.db import schema_readiness_issues

    settings = AppSettings()
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


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    errors: list[str] = []
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
            )
        )
        if agency_socket_is_socket:
            agency_health_error = _agency_health_error(REQUIRED_AGENCY_SOCKET_PATH)
            if agency_health_error is not None:
                errors.append(agency_health_error)
    except subprocess.CalledProcessError as exc:
        errors.append(f"systemctl check failed: {exc}")

    try:
        payload = _health_payload(str(args.health_url))
        if payload.get("ok") is not True:
            errors.append("health endpoint is not ok")
    except RuntimeError as exc:
        errors.append(str(exc))

    if args.check_db_schema:
        errors.extend(_schema_readiness_errors())

    if errors:
        print("production posture check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("production posture check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
