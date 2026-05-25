#!/usr/bin/env bash
set -euo pipefail

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
ok() { printf '\033[1;32m    ✓ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$1"; }
fail() { printf '\033[1;31m    ✗ %s\033[0m\n' "$1"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
INSTALL_ROOT="/opt/ariel"
ARIEL_STATE_ROOT="/var/lib/ariel"
AGENCY_ROOT="/opt/agency"
AGENCY_STATE_ROOT="/var/lib/agency"
ENV_DIR="/etc/ariel"
ENV_FILE="${ENV_DIR}/ariel.env"
SYSTEMD_SRC="${REPO_ROOT}/deploy/systemd"
SYSTEMD_DST="/etc/systemd/system"
SERVICES=(
  agency-daemon.service
  ariel-api.service
  ariel-worker.service
  ariel-pubsub.service
  ariel-discord.service
)

info "Preflight checks"
if [ "$(id -u)" -ne 0 ]; then
  fail "Must run as root: sudo bash scripts/install_production_services.sh"
  exit 1
fi
ok "running as root"

if [ "$REPO_ROOT" != "$INSTALL_ROOT" ]; then
  fail "Repository root is $REPO_ROOT, expected $INSTALL_ROOT"
  printf '  Pull the intended Ariel revision into %s, then rerun this script there.\n' "$INSTALL_ROOT"
  exit 1
fi
ok "repository root is $INSTALL_ROOT"

if [ ! -d "$SYSTEMD_SRC" ]; then
  fail "Missing checked-in systemd directory: $SYSTEMD_SRC"
  exit 1
fi
ok "checked-in systemd units present"

info "Ensuring ariel service account"
if ! id -u ariel >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_ROOT" --shell /usr/sbin/nologin ariel
  ok "created ariel user"
else
  ok "ariel user exists"
fi

info "Preparing canonical directories"
install -d -o root -g root -m 0755 "$INSTALL_ROOT"
install -d -o root -g root -m 0755 "$AGENCY_ROOT"
install -d -o ariel -g ariel -m 0750 "$ARIEL_STATE_ROOT" "${ARIEL_STATE_ROOT}/attachment-blobs"
install -d -o ariel -g ariel -m 0750 "$AGENCY_STATE_ROOT"
install -d -o root -g ariel -m 0750 "$ENV_DIR" "${ENV_DIR}/secrets"
ok "canonical directories ready"

if [ -e "$ENV_FILE" ]; then
  chown root:ariel "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  ok "$ENV_FILE ownership/mode repaired"
else
  install -o root -g ariel -m 0640 /dev/null "$ENV_FILE"
  warn "created empty $ENV_FILE; populate rotated secrets before starting services"
fi

info "Installing systemd units"
for service in "${SERVICES[@]}"; do
  install -o root -g root -m 0644 "${SYSTEMD_SRC}/${service}" "${SYSTEMD_DST}/${service}"
  ok "installed ${service}"
done
systemctl daemon-reload
ok "systemd daemon reloaded"

info "Installed but not started"
printf '  Populate %s, install runsc, install /opt/agency/bin/agency, run migrations, then:\n' "$ENV_FILE"
printf '  Ensure ARIEL_ATTACHMENT_BLOB_STORE_PATH=%s in %s.\n' "${ARIEL_STATE_ROOT}/attachment-blobs" "$ENV_FILE"
printf '    systemctl enable --now %s\n' "${SERVICES[*]}"
printf '    make production-posture\n'
