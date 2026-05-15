#!/usr/bin/env bash
set -euo pipefail

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
ok()    { printf '\033[1;32m    ✓ %s\033[0m\n' "$1"; }
fail()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$1"; }

errors=0
uv_cmd=""

# ── 1. Check prerequisites ─────────────────────────────────────────────
info "Checking prerequisites"

# Python 3.12+
if python3 -c "import sys; assert sys.version_info >= (3,12)" 2>/dev/null; then
  ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
else
  fail "Python 3.12+ required. Install from https://www.python.org/downloads/"
  errors=$((errors + 1))
fi

# UV
if command -v uv >/dev/null 2>&1; then
  uv_cmd=$(command -v uv)
  ok "uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
  uv_cmd="$HOME/.local/bin/uv"
  ok "uv"
else
  fail "uv required. Install from https://docs.astral.sh/uv/getting-started/installation/"
  errors=$((errors + 1))
fi

# Docker
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "docker"
else
  fail "Docker required and must be running. Install from https://docs.docker.com/get-docker/"
  errors=$((errors + 1))
fi

if [ "$errors" -gt 0 ]; then
  printf '\n\033[1;31mFix the above errors and re-run: make bootstrap\033[0m\n'
  exit 1
fi

# ── 2. Create venv & install deps ──────────────────────────────────────
info "Setting up Python environment"
PATH="$(dirname "$uv_cmd"):$PATH" make setup

# ── 3. Create .env.local ───────────────────────────────────────────────
info "Initializing environment config"
make env-init

# ── 4. Check API key ───────────────────────────────────────────────────
info "Checking API key"
api_key=$(grep -E '^ARIEL_OPENAI_API_KEY=' .env.local 2>/dev/null | cut -d= -f2-)
if [ -z "$api_key" ] || [ "$api_key" = "your_real_key" ]; then
  fail "ARIEL_OPENAI_API_KEY is still the placeholder value."
  printf '\n  Edit \033[1m.env.local\033[0m and set your real API key, then re-run:\n'
  printf '    make bootstrap\n\n'
  exit 1
fi
ok "API key is set"

# ── 5. Start database ──────────────────────────────────────────────────
info "Starting Postgres"
make db-up

# ── 6. Run migrations ──────────────────────────────────────────────────
info "Running database migrations"
make db-upgrade

# ── 7. Done ──────────────────────────────────────────────────────────────
info "Bootstrap complete"
printf '  Run \033[1mmake dev\033[0m to start the app.\n'
printf '  Run \033[1mmake tailscale-serve\033[0m to expose via Tailscale.\n\n'
