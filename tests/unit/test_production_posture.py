from __future__ import annotations

from ariel.production_posture import (
    ARIEL_SYSTEMD_SERVICES,
    REQUIRED_AGENCY_SOCKET_PATH,
    REQUIRED_ENVIRONMENT_FILE,
    REQUIRED_UNIT_PROPERTIES,
    validate_agency_daemon_posture,
    validate_production_service_posture,
)


def _canonical_properties() -> dict[str, dict[str, str]]:
    return {
        service: {
            **REQUIRED_UNIT_PROPERTIES,
            "EnvironmentFiles": f"{REQUIRED_ENVIRONMENT_FILE} (ignore_errors=no)",
        }
        for service in ARIEL_SYSTEMD_SERVICES
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
    properties["ariel-worker"]["EnvironmentFiles"] = ""

    errors = validate_production_service_posture(
        active_states={service: "active" for service in ARIEL_SYSTEMD_SERVICES},
        enabled_states={
            service: "enabled" if service != "ariel-pubsub" else "disabled"
            for service in ARIEL_SYSTEMD_SERVICES
        },
        unit_properties=properties,
    )

    assert "ariel-api User is 'niels', expected 'ariel'" in errors
    assert "ariel-pubsub enabled state is 'disabled', expected 'enabled'" in errors
    assert "ariel-worker EnvironmentFiles does not include /etc/ariel/ariel.env" in errors


def test_validate_agency_daemon_posture_accepts_canonical_service() -> None:
    assert (
        validate_agency_daemon_posture(
            active_state="active",
            enabled_state="enabled",
            socket_is_socket=True,
        )
        == []
    )


def test_validate_agency_daemon_posture_reports_drift() -> None:
    errors = validate_agency_daemon_posture(
        active_state="inactive",
        enabled_state="disabled",
        socket_is_socket=False,
    )

    assert "agency-daemon active state is 'inactive', expected 'active'" in errors
    assert "agency-daemon enabled state is 'disabled', expected 'enabled'" in errors
    assert f"{REQUIRED_AGENCY_SOCKET_PATH} is not a Unix socket" in errors
