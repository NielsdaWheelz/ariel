from __future__ import annotations

from collections.abc import Mapping


ARIEL_SYSTEMD_SERVICES: tuple[str, ...] = (
    "ariel-api",
    "ariel-worker",
    "ariel-pubsub",
    "ariel-discord",
)
AGENCY_DAEMON_SERVICE = "agency-daemon"
AGENCY_INSTALL_ROOT = "/opt/agency"
AGENCY_STATE_ROOT = "/var/lib/agency"
AGENCY_DATA_DIR = AGENCY_STATE_ROOT
REQUIRED_AGENCY_SOCKET_PATH = f"{AGENCY_DATA_DIR}/agencyd.sock"

REQUIRED_UNIT_PROPERTIES: dict[str, str] = {
    "User": "ariel",
    "WorkingDirectory": "/opt/ariel",
    "NoNewPrivileges": "yes",
    "ProtectSystem": "full",
    "ProtectHome": "yes",
}

REQUIRED_ENVIRONMENT_FILE = "/etc/ariel/ariel.env"


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
    return errors


def validate_agency_daemon_posture(
    *,
    active_state: str | None,
    enabled_state: str | None,
    socket_is_socket: bool,
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
    return errors
