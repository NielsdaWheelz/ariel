from __future__ import annotations

from pathlib import Path
import re

from ariel.production_posture import (
    AGENCY_DATA_DIR,
    AGENCY_DAEMON_SERVICE,
    AGENCY_INSTALL_ROOT,
    AGENCY_STATE_ROOT,
    REQUIRED_AGENCY_SOCKET_PATH,
)


ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "deploy/caddy/Caddyfile"
CADDY_INSTALL = ROOT / "deploy/caddy/install.sh"
RUNBOOK = ROOT / "docs/production-runbook.md"
SYSTEMD_DIR = ROOT / "deploy/systemd"
ARIEL_SERVICE_FILES = {
    "ariel-api.service",
    "ariel-discord.service",
    "ariel-pubsub.service",
    "ariel-worker.service",
}
AGENCY_SERVICE_FILE = f"{AGENCY_DAEMON_SERVICE}.service"
REQUIRED_SERVICE_LINES = {
    "User=ariel",
    "Group=ariel",
    "WorkingDirectory=/opt/ariel",
    "EnvironmentFile=/etc/ariel/ariel.env",
    "Restart=always",
    "RestartSec=5",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectSystem=full",
    "ProtectHome=true",
    "ProtectControlGroups=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
    "CapabilityBoundingSet=",
    "SystemCallArchitectures=native",
}
REQUIRED_AGENCY_SERVICE_LINES = {
    "User=ariel",
    "Group=ariel",
    f"WorkingDirectory={AGENCY_INSTALL_ROOT}",
    "StateDirectory=agency",
    f"Environment=AGENCY_DATA_DIR={AGENCY_DATA_DIR}",
    f"Environment=HOME={AGENCY_STATE_ROOT}",
    f"Environment=CODEX_HOME={AGENCY_STATE_ROOT}/.codex",
    f"ExecStart={AGENCY_INSTALL_ROOT}/bin/agency daemon start --foreground",
    "Restart=always",
    "RestartSec=5",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectSystem=full",
    "ProtectHome=true",
    "ProtectControlGroups=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
    "CapabilityBoundingSet=",
    "SystemCallArchitectures=native",
    f"ReadWritePaths={AGENCY_STATE_ROOT}",
}
CADDY_PUBLIC_PROXY_PATHS = {
    "/v1/providers/google/events",
    "/v1/connectors/google/callback",
}


def _service_lines(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _caddy_site_hosts(text: str) -> set[str]:
    return set(re.findall(r"^([a-z0-9][a-z0-9.-]+\.[a-z]{2,}) \{$", text, re.MULTILINE))


def test_ariel_systemd_units_have_canonical_runtime_and_hardening() -> None:
    assert {path.name for path in SYSTEMD_DIR.glob("*.service")} == (
        ARIEL_SERVICE_FILES | {AGENCY_SERVICE_FILE}
    )

    for service_name in ARIEL_SERVICE_FILES:
        lines = _service_lines(SYSTEMD_DIR / service_name)
        assert REQUIRED_SERVICE_LINES.issubset(lines), service_name


def test_agency_systemd_unit_matches_production_contract() -> None:
    lines = _service_lines(SYSTEMD_DIR / AGENCY_SERVICE_FILE)
    assert REQUIRED_AGENCY_SERVICE_LINES.issubset(lines)

    runbook_text = RUNBOOK.read_text(encoding="utf-8")
    assert f"- `{AGENCY_SERVICE_FILE}`" in runbook_text
    assert f"Agency root: `{AGENCY_INSTALL_ROOT}`" in runbook_text
    assert f"Agency state root: `{AGENCY_STATE_ROOT}`" in runbook_text
    assert f"Agency socket: `{REQUIRED_AGENCY_SOCKET_PATH}`" in runbook_text


def test_caddyfile_exposes_only_google_callback_routes() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")

    proxied_paths = set(
        re.findall(r"handle (/[^ ]+) \{\n\s+reverse_proxy 127\.0\.0\.1:8000\n\s+\}", text)
    )
    assert proxied_paths == CADDY_PUBLIC_PROXY_PATHS
    assert text.count("reverse_proxy 127.0.0.1:8000") == len(CADDY_PUBLIC_PROXY_PATHS)
    assert re.search(r"handle \{\n\s+respond 404\n\s+\}", text) is not None
    assert "replace code REDACTED" in text
    assert "replace state REDACTED" in text


def test_caddy_installer_matches_checked_in_site_host_and_enables_service() -> None:
    caddy_text = CADDYFILE.read_text(encoding="utf-8")
    install_text = CADDY_INSTALL.read_text(encoding="utf-8")
    site_hosts = _caddy_site_hosts(caddy_text)

    assert len(site_hosts) == 1
    assert next(iter(site_hosts)) in install_text
    assert "systemctl enable --now caddy" in install_text
    assert "ufw allow 80/tcp" in install_text
    assert "ufw allow 443/tcp" in install_text


def test_runbook_uses_post_callback_smoke_not_unauthenticated_head() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "curl -I https://<your-fqdn>/v1/providers/google/events" not in text
    assert "-X POST 'https://<your-fqdn>/v1/providers/google/events" in text
    assert "422 without Google watch headers" in text
