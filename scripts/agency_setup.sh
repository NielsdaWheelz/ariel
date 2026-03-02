#!/usr/bin/env bash
set -euo pipefail

# Ensure Homebrew is on PATH (macOS) — needed when agency runs
# this script via `sh -lc` which doesn't source .zshrc/.zprofile.
if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi

# Copy .env.local from the source repo so the worktree inherits
# real secrets (the file is gitignored so it won't exist otherwise).
if [ ! -f .env.local ] && [ -f ../../repo.json ]; then
  source_repo=$(python3 -c \
    "import json,pathlib; print(json.load(open('../../repo.json'))['repo_root_last_seen'])" \
    2>/dev/null || true)
  if [ -n "$source_repo" ] && [ -f "$source_repo/.env.local" ]; then
    cp "$source_repo/.env.local" .env.local
  fi
fi

make setup
make env-init
make db-up
make db-upgrade
