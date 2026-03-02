#!/usr/bin/env bash
set -euo pipefail

# Ensure Homebrew is on PATH (macOS) — needed when agency runs
# this script via `sh -lc` which doesn't source .zshrc/.zprofile.
if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi

make bootstrap
