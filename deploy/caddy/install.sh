#!/usr/bin/env bash
set -euo pipefail

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
ok()    { printf '\033[1;32m    ✓ %s\033[0m\n' "$1"; }
fail()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$1"; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_CADDYFILE="${REPO_ROOT}/deploy/caddy/Caddyfile"
DST_CADDYFILE="/etc/caddy/Caddyfile"

# ── 1. Preflight ───────────────────────────────────────────────────────
info "Preflight checks"
if [ "$(id -u)" -ne 0 ]; then fail "Must run as root (sudo)."; exit 1; fi
ok "running as root"

if [ ! -f "$SRC_CADDYFILE" ]; then fail "Missing $SRC_CADDYFILE"; exit 1; fi
ok "source Caddyfile present"

port80_owner="$(ss -lntp 2>/dev/null | awk '$4 ~ /:80$/' | grep -oE 'users:\(\("[^"]+"' | head -n1 | sed 's/.*"//' || true)"
if [ -n "$port80_owner" ] && [ "$port80_owner" != "caddy" ]; then
  fail "Port 80 is held by '$port80_owner' (expected unused or 'caddy')."; exit 1
fi
ok "port 80 free or owned by caddy"

# ── 2. Install Caddy from Cloudsmith apt repo ──────────────────────────
info "Installing Caddy from official apt repo"
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy
ok "caddy installed: $(caddy version | awk '{print $1}')"

# ── 3. Deploy Caddyfile ────────────────────────────────────────────────
info "Deploying Caddyfile to $DST_CADDYFILE"
mkdir -p /etc/caddy
if [ -f "$DST_CADDYFILE" ] && ! cmp -s "$SRC_CADDYFILE" "$DST_CADDYFILE"; then
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$DST_CADDYFILE" "/etc/caddy/Caddyfile.bak-${ts}"
  ok "backed up existing Caddyfile to /etc/caddy/Caddyfile.bak-${ts}"
fi
cp "$SRC_CADDYFILE" "$DST_CADDYFILE"
caddy validate --config "$DST_CADDYFILE"
ok "Caddyfile validated"

# ── 4. Bootstrap log directory & files ─────────────────────────────────
# Caddy's shipped unit runs as user 'caddy' but does NOT declare
# LogsDirectory=, so on a fresh box the first writer can race and leave
# /var/log/caddy/<file> as a root:root 0600 stub — every later reload
# then fails with "permission denied". Pre-creating the dir and each
# referenced log file with caddy:caddy ownership closes the race.
# Keep CADDY_LOG_FILES in sync with `output file` paths in Caddyfile.
info "Bootstrapping /var/log/caddy"
id -u caddy >/dev/null 2>&1 || { fail "caddy user missing (apt install failed?)"; exit 1; }
getent group caddy >/dev/null 2>&1 || { fail "caddy group missing (apt install failed?)"; exit 1; }
install -d -o caddy -g caddy -m 0755 /var/log/caddy
ok "/var/log/caddy ready (caddy:caddy 0755)"
CADDY_LOG_FILES=(
  /var/log/caddy/ariel-webhook.log
)
for f in "${CADDY_LOG_FILES[@]}"; do
  if [ ! -e "$f" ]; then
    install -o caddy -g caddy -m 0640 /dev/null "$f"
    ok "created $f (caddy:caddy 0640)"
  else
    # Only fix ownership/mode if drifted; never truncate existing logs.
    cur="$(stat -c '%U:%G:%a' "$f")"
    if [ "$cur" != "caddy:caddy:640" ]; then
      chown caddy:caddy "$f"
      chmod 0640 "$f"
      ok "repaired $f ownership/mode (was $cur)"
    else
      ok "$f already caddy:caddy 0640"
    fi
  fi
done

# ── 5. Open UFW ports (if active) ──────────────────────────────────────
info "Configuring firewall"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  ok "ufw allows 80,443/tcp"
else
  ok "ufw not active; skipping (operator may use a different firewall)"
fi

# ── 6. Enable & reload caddy ───────────────────────────────────────────
info "Starting Caddy"
systemctl enable --now caddy
systemctl reload caddy
ok "caddy enabled and reloaded"

# ── 7. Done ────────────────────────────────────────────────────────────
info "Caddy is running on 80/443; verify with: curl -I https://ariel.nielseriknandal.com/"
