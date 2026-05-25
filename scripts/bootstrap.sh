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

# ── 4. Check current model provider keys ───────────────────────────────
info "Checking current model provider keys"
missing_model_key_report="$(
  .venv/bin/python - <<'PY'
from pathlib import Path

from ariel.dev_db import load_local_env
from ariel.models import required_model_provider_env_vars
from ariel.production_posture import validate_required_environment_values

env = load_local_env(Path.cwd())

for error in validate_required_environment_values(
    values=env,
    required_env_vars=required_model_provider_env_vars(),
    source_label="active env stack",
):
    print(error)
PY
)"
missing_model_keys=()
while IFS= read -r missing_key_error; do
  if [ -n "$missing_key_error" ]; then
    fail "$missing_key_error"
    missing_model_keys+=("$missing_key_error")
  fi
done <<< "$missing_model_key_report"
if [ "${#missing_model_keys[@]}" -gt 0 ]; then
  printf '\n  Edit the active local env file stack (\033[1m.env\033[0m + \033[1m.env.local\033[0m, or \033[1mARIEL_ENV_FILE\033[0m when set) and set the current model provider keys, then re-run:\n'
  printf '    make bootstrap\n\n'
  exit 1
fi
ok "current model provider keys are set"

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
