from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from ariel.dev_db import parse_dotenv_file, resolve_local_postgres_runtime
from ariel.production_posture import (
    AGENCY_DATA_DIR,
    AGENCY_DAEMON_SERVICE,
    AGENCY_INSTALL_ROOT,
    AGENCY_STATE_ROOT,
    ARIEL_STATE_ROOT,
    CADDY_SERVICE,
    REQUIRED_ATTACHMENT_BLOB_STORE_PATH,
    REQUIRED_CADDY_PUBLIC_PROXY_ROUTES,
    REQUIRED_AGENCY_SOCKET_PATH,
    REQUIRED_PUBLIC_FIREWALL_PORTS,
    REQUIRED_UNIT_EXECSTART_SUBSTRINGS,
)


ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = ROOT / "deploy/caddy/Caddyfile"
CADDY_INSTALL = ROOT / "deploy/caddy/install.sh"
VERIFY_PRODUCTION_POSTURE = ROOT / "scripts/verify_production_posture.py"
GCP_CREATE_RUNTIME_SA = ROOT / "scripts/gcp_create_runtime_sa.sh"
RUNBOOK = ROOT / "docs/production-runbook.md"
DEV_ENV_DOC = ROOT / "docs/dev-environment.md"
DEV_ENV_EXAMPLE = ROOT / ".env.dev.example"
BOOTSTRAP = ROOT / "scripts/bootstrap.sh"
PRODUCTION_SERVICES_INSTALLER = ROOT / "scripts/install_production_services.sh"
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
REQUIRED_ARIEL_SERVICE_SPECIFIC_LINES = {
    "ariel-api.service": {
        "After=network-online.target postgresql.service",
        "Wants=network-online.target postgresql.service",
        (
            "ExecStart=/bin/sh -c 'exec "
            f"{REQUIRED_UNIT_EXECSTART_SUBSTRINGS['ariel-api']} "
            '--host "$${ARIEL_BIND_HOST}" --port "$${ARIEL_BIND_PORT}"\''
        ),
    },
    "ariel-worker.service": {
        "After=network-online.target postgresql.service ariel-api.service",
        "Wants=network-online.target postgresql.service ariel-api.service",
        f"ExecStart={REQUIRED_UNIT_EXECSTART_SUBSTRINGS['ariel-worker']}",
    },
    "ariel-pubsub.service": {
        "After=network-online.target postgresql.service ariel-api.service",
        "Wants=network-online.target postgresql.service ariel-api.service",
        f"ExecStart={REQUIRED_UNIT_EXECSTART_SUBSTRINGS['ariel-pubsub']}",
    },
    "ariel-discord.service": {
        "After=network-online.target ariel-api.service",
        "Wants=network-online.target ariel-api.service",
        f"ExecStart={REQUIRED_UNIT_EXECSTART_SUBSTRINGS['ariel-discord']}",
    },
}
CADDY_PUBLIC_PROXY_ROUTES = set(REQUIRED_CADDY_PUBLIC_PROXY_ROUTES)


def _service_lines(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _caddy_site_hosts(text: str) -> set[str]:
    return set(re.findall(r"^([a-z0-9][a-z0-9.-]+\.[a-z]{2,}) \{$", text, re.MULTILINE))


def _caddy_proxied_routes(text: str) -> set[tuple[str, str]]:
    matcher_routes: dict[str, tuple[str, str]] = {}
    for name, body in re.findall(
        r"^\s*@([A-Za-z0-9_]+)\s+\{\n(?P<body>.*?)^\s*\}",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        method_match = re.search(r"^\s*method\s+([A-Z]+)\s*$", body, re.MULTILINE)
        path_match = re.search(r"^\s*path\s+([^\s]+)\s*$", body, re.MULTILINE)
        if method_match is not None and path_match is not None:
            matcher_routes[name] = (method_match.group(1), path_match.group(1))

    proxied_routes: set[tuple[str, str]] = set()
    for name in re.findall(
        r"^\s*handle\s+@([A-Za-z0-9_]+)\s+\{\n\s+reverse_proxy 127\.0\.0\.1:8000\n\s+\}",
        text,
        re.MULTILINE,
    ):
        proxied_routes.add(matcher_routes[name])
    return proxied_routes


def _caddy_log_files(text: str) -> set[str]:
    return set(re.findall(r"^\s+output file (/[^ ]+)", text, re.MULTILINE))


def test_ariel_systemd_units_have_canonical_runtime_and_hardening() -> None:
    assert {path.name for path in SYSTEMD_DIR.glob("*.service")} == (
        ARIEL_SERVICE_FILES | {AGENCY_SERVICE_FILE}
    )

    for service_name in ARIEL_SERVICE_FILES:
        lines = _service_lines(SYSTEMD_DIR / service_name)
        assert REQUIRED_SERVICE_LINES.issubset(lines), service_name
        assert REQUIRED_ARIEL_SERVICE_SPECIFIC_LINES[service_name].issubset(lines), service_name


def test_agency_systemd_unit_matches_production_contract() -> None:
    lines = _service_lines(SYSTEMD_DIR / AGENCY_SERVICE_FILE)
    assert REQUIRED_AGENCY_SERVICE_LINES.issubset(lines)

    runbook_text = RUNBOOK.read_text(encoding="utf-8")
    assert f"- `{AGENCY_SERVICE_FILE}`" in runbook_text
    assert f"App state root: `{ARIEL_STATE_ROOT}`" in runbook_text
    assert f"Agency root: `{AGENCY_INSTALL_ROOT}`" in runbook_text
    assert f"Agency state root: `{AGENCY_STATE_ROOT}`" in runbook_text
    assert f"Agency socket: `{REQUIRED_AGENCY_SOCKET_PATH}`" in runbook_text


def test_caddyfile_exposes_only_google_callback_routes() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")

    assert _caddy_proxied_routes(text) == CADDY_PUBLIC_PROXY_ROUTES
    assert text.count("reverse_proxy 127.0.0.1:8000") == len(CADDY_PUBLIC_PROXY_ROUTES)
    assert re.search(r"handle \{\n\s+respond 404\n\s+\}", text) is not None
    assert "replace code REDACTED" in text
    assert "replace state REDACTED" in text
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in text
    assert 'X-Content-Type-Options "nosniff"' in text
    assert 'Referrer-Policy "no-referrer"' in text
    assert "-Server" in text
    assert "trusted_proxies" not in text


def test_caddy_installer_matches_checked_in_site_host_and_enables_service() -> None:
    caddy_text = CADDYFILE.read_text(encoding="utf-8")
    install_text = CADDY_INSTALL.read_text(encoding="utf-8")
    site_hosts = _caddy_site_hosts(caddy_text)

    assert len(site_hosts) == 1
    assert next(iter(site_hosts)) in install_text
    assert "systemctl enable --now caddy" in install_text
    assert "ufw allow 80/tcp" in install_text
    assert "ufw allow 443/tcp" in install_text


def test_caddy_installer_validates_before_replacing_live_config_and_opening_firewall() -> None:
    text = CADDY_INSTALL.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(CADDY_INSTALL)], check=True)
    assert "check_port_owner 80" in text
    assert "check_port_owner 443" in text
    assert 'cp "$SRC_CADDYFILE" "$DST_CADDYFILE"' not in text
    assert text.index("install -d -o caddy -g caddy -m 0750 /var/log/caddy") < text.index(
        'caddy validate --config "$SRC_CADDYFILE"'
    )
    assert text.index('caddy validate --config "$tmp_caddyfile"') < text.index(
        'mv "$tmp_caddyfile" "$DST_CADDYFILE"'
    )
    assert text.index("systemctl reload caddy") < text.index("ufw allow 80/tcp")


def test_caddy_installer_log_file_list_tracks_caddyfile_outputs() -> None:
    caddy_text = CADDYFILE.read_text(encoding="utf-8")
    install_text = CADDY_INSTALL.read_text(encoding="utf-8")
    match = re.search(r"CADDY_LOG_FILES=\(\n(?P<body>.*?)\n\)", install_text, re.DOTALL)

    assert match is not None
    installer_log_files = set(re.findall(r"^\s+(/[^\s]+)\s*$", match.group("body"), re.MULTILINE))
    assert installer_log_files == _caddy_log_files(caddy_text)


def test_production_posture_verifier_checks_live_caddy_and_firewall_contract() -> None:
    text = VERIFY_PRODUCTION_POSTURE.read_text(encoding="utf-8")

    assert CADDY_SERVICE in text
    assert "REQUIRED_CADDYFILE_PATH" in text
    for port in REQUIRED_PUBLIC_FIREWALL_PORTS:
        assert str(port) in text
    assert "validate_caddy_service_posture" in text
    assert "validate_caddy_config_posture" in text
    assert "validate_ufw_firewall_posture" in text
    assert "deploy/caddy/Caddyfile" in text
    assert "_health_posture_errors" in text
    assert "provider_evidence" in text
    assert "cap.provider_evidence.read" in text


def test_runbook_uses_post_callback_smoke_not_unauthenticated_head() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "curl -I https://<your-fqdn>/v1/providers/google/events" not in text
    assert "-X POST 'https://<your-fqdn>/v1/providers/google/events" in text
    assert "422 without Google watch headers" in text


def test_runbook_documents_production_posture_env_file_key_check() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "required production env vars" in text
    assert "current checked-in model" in text
    assert "production `AppSettings` source" in text
    assert "unknown `ARIEL_*` names" in text
    assert "/etc/ariel/ariel.env" in text
    assert f"ARIEL_ATTACHMENT_BLOB_STORE_PATH={REQUIRED_ATTACHMENT_BLOB_STORE_PATH}" in text


def test_dev_environment_docs_use_canonical_production_postgres_shape() -> None:
    doc_text = DEV_ENV_DOC.read_text(encoding="utf-8")
    example_text = DEV_ENV_EXAMPLE.read_text(encoding="utf-8")
    example_env = parse_dotenv_file(DEV_ENV_EXAMPLE)
    runtime = resolve_local_postgres_runtime(example_env)

    assert "postgresql.service" in doc_text
    assert "127.0.0.1:5432" in doc_text
    assert "canonical production Postgres service" in example_text
    assert (
        f"| Postgres service    | `postgresql.service`                 | `{runtime.container_name}`"
    ) in doc_text
    assert (
        f"| Postgres port       | `127.0.0.1:5432`                     | "
        f"`127.0.0.1:{runtime.host_port}`"
    ) in doc_text
    assert (
        f"| Postgres data       | host-managed Postgres data dir       | `{runtime.volume_name}`"
    ) in doc_text
    assert (
        f"| API bind            | `127.0.0.1:8000`                     | "
        f"`{example_env['ARIEL_BIND_HOST']}:{example_env['ARIEL_BIND_PORT']}`"
    ) in doc_text
    for stale_phrase in (
        "prod DB (`ariel-postgres` on :5433)",
        "ariel-postgres` (prod, :5433)",
        "ariel-postgres` on :5433",
        "psql -p 5433",
        "make db-status         # ariel-postgres on :5433",
        "prod container",
    ):
        assert stale_phrase not in doc_text
        assert stale_phrase not in example_text


def test_bootstrap_model_key_check_uses_shared_env_parser_and_project_root() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=True)
    assert "load_local_env(Path.cwd())" in text
    assert "validate_required_environment_values" in text


def test_production_posture_script_has_redacted_env_audit_mode() -> None:
    text = VERIFY_PRODUCTION_POSTURE.read_text(encoding="utf-8")

    assert "--redacted-env-audit" in text
    assert "--env-file" in text
    assert "load_local_env(_repo_root())" in text
    assert "parse_environment_file(env_path.read_text" in text
    assert "REQUIRED_PRODUCTION_ENV_VARS + required_model_provider_env_vars()" in text
    assert "validate_production_environment_settings" in text
    assert "redacted_local_environment_audit" in text


def test_redacted_env_audit_reports_missing_explicit_env_file_cleanly(tmp_path: Path) -> None:
    missing_env = tmp_path / "missing.env"

    completed = subprocess.run(
        [
            sys.executable,
            str(VERIFY_PRODUCTION_POSTURE),
            "--redacted-env-audit",
            "--env-file",
            str(missing_env),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 1
    assert "redacted env audit:" in completed.stdout
    assert "--env-file points to missing env file" in completed.stdout
    assert str(missing_env) in completed.stdout
    assert "Traceback" not in completed.stdout
    assert "Traceback" not in completed.stderr


def test_production_services_installer_matches_systemd_contract() -> None:
    text = PRODUCTION_SERVICES_INSTALLER.read_text(encoding="utf-8")
    runbook_text = RUNBOOK.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(PRODUCTION_SERVICES_INSTALLER)], check=True)
    assert 'INSTALL_ROOT="/opt/ariel"' in text
    assert 'ARIEL_STATE_ROOT="/var/lib/ariel"' in text
    assert 'AGENCY_ROOT="/opt/agency"' in text
    assert 'AGENCY_STATE_ROOT="/var/lib/agency"' in text
    assert 'ENV_FILE="${ENV_DIR}/ariel.env"' in text
    assert 'install -d -o root -g root -m 0755 "$INSTALL_ROOT"' in text
    assert 'install -d -o root -g root -m 0755 "$AGENCY_ROOT"' in text
    assert (
        'install -d -o ariel -g ariel -m 0750 "$ARIEL_STATE_ROOT" '
        '"${ARIEL_STATE_ROOT}/attachment-blobs"'
    ) in text
    for service_name in ARIEL_SERVICE_FILES | {AGENCY_SERVICE_FILE}:
        assert service_name in text
    assert "systemctl daemon-reload" in text
    assert re.search(r"^systemctl enable --now", text, re.MULTILINE) is None
    assert "scripts/install_production_services.sh" in runbook_text


def test_gcp_runtime_sa_script_points_operator_at_production_secret_path() -> None:
    text = GCP_CREATE_RUNTIME_SA.read_text(encoding="utf-8")
    runbook_text = RUNBOOK.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(GCP_CREATE_RUNTIME_SA)], check=True)
    assert 'PRODUCTION_KEY_PATH="/etc/ariel/secrets/gcp-pubsub-sa.json"' in text
    assert "Production env value for ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH" in text
    assert "/etc/ariel/secrets/gcp-pubsub-sa.json" in runbook_text
